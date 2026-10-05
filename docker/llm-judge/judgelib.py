"""Pure helpers for the llm-judge sidecar. Stdlib only (python-docx is imported lazily)."""

from __future__ import annotations

import json
import mmap
import re
import subprocess
from collections.abc import Callable, Mapping
from email.message import Message
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any

TEXT_EXT = {
    ".py", ".cpp", ".h", ".hpp", ".c", ".java", ".js", ".ts", ".md", ".txt", ".json",
    ".yml", ".yaml", ".csv", ".sql", ".html", ".css",
}  # fmt: skip
MAX_FILE_CHARS = 150_000
MAX_TOTAL_CHARS = 400_000
TRUNCATED = "\n[truncated]"
COPY_CHUNK = 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIN_PAGE_CHARS = 200
DISALLOWED_TOOLS = "Bash,Edit,Write,WebFetch,WebSearch,NotebookEdit,Task,Agent"


def safe_name(name: str, idx: int) -> str:
    """Display/disk name: index prefix + sanitised basename. Never the raw filename."""
    base = re.split(r"[\\/]", name)[-1]
    return f"{idx:02d}_" + re.sub(r"[^\w.\-]+", "_", base)[:100]


def pdf_needs_visual(page_texts: list[str]) -> bool:
    return any(len(re.sub(r"\s", "", p)) < MIN_PAGE_CHARS for p in page_texts)


def _pdf_text(path: Path) -> tuple[str | None, bool]:
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        return None, True  # let Claude try the Read tool instead
    if proc.returncode != 0:
        return None, True
    pages = proc.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    text = proc.stdout.replace("\f", "\n").strip()
    return (text or None), pdf_needs_visual(pages) if pages else True


def _docx_text(path: Path) -> str | None:
    import docx  # python3-docx

    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts).strip() or None


def extract(path: Path, mime: str) -> tuple[str | None, bool]:
    """(text or None, offer_for_read)."""
    ext = path.suffix.lower()
    if ext == ".pdf" or mime == "application/pdf":
        return _pdf_text(path)
    if ext == ".docx":
        try:
            return _docx_text(path), False
        except Exception:  # corrupt/zip-bomb-ish file: treat as unreadable
            return None, False
    if ext in TEXT_EXT:
        with path.open("rb") as fh:  # 4 bytes/char is the worst case for the char cap
            return fh.read(MAX_FILE_CHARS * 4).decode("utf-8", errors="replace"), False
    if ext in IMAGE_EXT:
        return None, True
    return None, False


def cap_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(limit, 0)] + TRUNCATED, True


def split_multipart(
    path: Path,
    content_type: str,
    dest_for: Callable[[int, str], Path],
    max_field: int = 2 * 1024 * 1024,
) -> tuple[dict[str, str], list[tuple[str, str, Path]]]:
    """Split a multipart/form-data body stored in ``path`` without loading it.

    File parts (field ``files``) are copied in chunks to ``dest_for(idx, filename)``;
    other parts are small text fields held in memory. Raises ValueError if malformed.
    """
    holder = Message()
    holder["Content-Type"] = content_type
    boundary = holder.get_boundary()
    if not boundary:
        raise ValueError("no multipart boundary")
    delim = b"--" + boundary.encode()
    if path.stat().st_size == 0:
        raise ValueError("empty body")
    fields: dict[str, str] = {}
    files: list[tuple[str, str, Path]] = []
    with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:

        def find_delim(start: int, needle: bytes = delim) -> int:
            pos = mm.find(needle, start)
            while pos != -1 and mm[pos + len(needle) : pos + len(needle) + 2] not in (
                b"\r\n",
                b"--",
            ):
                pos = mm.find(needle, pos + 1)  # "--Bx" is content, not our boundary
            return pos

        pos = find_delim(0)
        while pos != -1:
            pos += len(delim)
            if mm[pos : pos + 2] == b"--":
                return fields, files
            pos += 2  # CRLF after the delimiter line
            hend = mm.find(b"\r\n\r\n", pos)
            if hend == -1 or hend - pos > 16 * 1024:
                raise ValueError("bad part headers")
            part = BytesParser(policy=HTTP).parsebytes(mm[pos:hend] + b"\r\n\r\n")
            start = hend + 4
            nxt = find_delim(start, b"\r\n" + delim)
            if nxt == -1:
                raise ValueError("multipart body not terminated")
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            if filename is not None and name == "files":
                dest = dest_for(len(files) + 1, filename)
                with dest.open("wb") as out:
                    for off in range(start, nxt, COPY_CHUNK):
                        out.write(mm[off : min(off + COPY_CHUNK, nxt)])
                files.append((filename, part.get_content_type(), dest))
            elif name and filename is None:
                if nxt - start > max_field:
                    raise ValueError("form field too large")
                fields[str(name)] = mm[start:nxt].decode("utf-8", errors="replace")
            pos = nxt + 2  # skip the CRLF that precedes the delimiter
    raise ValueError("multipart body not terminated")


def build_prompt_with_files(
    prompt: str, files: list[tuple[str, str | None, bool]], root: Path = Path("/tmp/judge")
) -> str:
    """Append every file after the unchanged prompt (stable prefix keeps prompt caching).

    ``files`` items are (safe_name, text, offer_for_read); the file lives at
    ``root / safe_name`` (root = the request's work dir) for the Read-tool pointer.
    """
    out = [prompt]
    for name, text, offered in files:
        out.append(f"\n--- FILE: {name} ---\n")
        if text is not None:
            out.append(text)
        elif offered:
            out.append("[no text layer]")
        else:
            out.append("[unsupported file type]")
        if offered:
            out.append(f"\n[You may open this file with the Read tool: {root / name}]")
    return "".join(out)


def cli_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Environment for the claude subprocess: the sidecar's own secrets (LLM_JUDGE_*,
    above all the bearer token) are dropped so a prompt-injected read of
    /proc/self/environ finds nothing; HOME/PATH/CLAUDE_CONFIG_DIR/LANG pass through."""
    return {k: v for k, v in environ.items() if not k.startswith("LLM_JUDGE_")}


def claude_argv(
    model: str, system: str, workdir: Path, allow_read: bool, n_offered: int = 0
) -> list[str]:
    """Allow-list first: ``--tools`` leaves only Read (or nothing); the deny-list is a
    second layer. No settings files, MCP servers or session files are picked up.

    Read is deliberately NOT pre-approved via ``--allowedTools``: a bare ``Read`` rule
    approves every path (credentials, /proc). Headless ``-p`` reads inside the working
    dirs (cwd + ``--add-dir``) without a prompt and denies anything outside, and
    ``--restricted`` additionally confines the file tools to the working dirs."""
    turns = max(6, 2 + n_offered)
    argv = [
        "claude", "-p", "--output-format", "json", "--model", model,
        "--append-system-prompt", system, "--max-turns", str(turns), "--add-dir", str(workdir),
        "--tools", "Read" if allow_read else "",
        "--restricted", "--strict-mcp-config", "--setting-sources", "",
        "--no-session-persistence", "--disallowedTools", DISALLOWED_TOOLS,
    ]  # fmt: skip
    return argv


def parse_cli_output(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise ValueError("claude output is not JSON") from exc
    if isinstance(data, list):  # some versions emit an event array; take the result event
        data = next((e for e in reversed(data) if isinstance(e, dict) and "result" in e), {})
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        raise ValueError("claude output has no string 'result'")
    if data.get("is_error"):
        raise ValueError(f"claude reported an error: {data['result'][:300]}")
    usage = data.get("usage")
    used = data.get("modelUsage")
    model = data.get("model") or (next(iter(used)) if isinstance(used, dict) and used else "")
    return {
        "result": data["result"],
        "model": model,
        "usage": usage if isinstance(usage, dict) else {},
    }
