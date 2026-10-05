"""HTTP front for `claude -p`: POST /grade (multipart), GET /health. Stdlib only."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from judgelib import (
    build_prompt_with_files,
    claude_argv,
    extract,
    parse_cli_output,
    safe_name,
)

MAX_BODY = 60 * 1024 * 1024
WORK_ROOT = os.environ.get("LLM_JUDGE_WORK_ROOT", "/tmp/judge")
_busy = threading.Lock()


def parse_multipart(content_type: str, body: bytes):
    """Return ({field: str}, [(filename, mime, bytes)]) from a multipart/form-data body."""
    msg = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    files: list[tuple[str, str, bytes]] = []
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None and name == "files":
            files.append((filename, part.get_content_type(), payload))
        elif name and filename is None:
            fields[str(name)] = payload.decode("utf-8", errors="replace")
    return fields, files


def grade(fields: dict[str, str], files: list[tuple[str, str, bytes]]) -> dict:
    Path(WORK_ROOT).mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=WORK_ROOT))
    try:
        entries: list[tuple[str, str | None, bool]] = []
        for idx, (filename, mime, data) in enumerate(files, 1):
            name = safe_name(filename, idx)
            path = workdir / name
            path.write_bytes(data)
            text, offered = extract(path, mime)
            entries.append((name, text, offered))
        prompt = build_prompt_with_files(fields.get("prompt", ""), entries, workdir)
        model = fields.get("model") or os.environ.get("LLM_JUDGE_MODEL") or "opus"
        argv = claude_argv(model, fields.get("system", ""), workdir, any(e[2] for e in entries))
        timeout = float(os.environ.get("LLM_JUDGE_CLI_TIMEOUT") or 540)
        proc = subprocess.run(
            argv, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=workdir
        )
        try:
            out = parse_cli_output(proc.stdout)  # is_error results raise with the message
        except ValueError as exc:
            raise RuntimeError(f"{exc} {proc.stderr[-300:]}".strip()) from exc
        out["model"] = out["model"] or model
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        home = Path.home()
        # ~/.claude.json is written by any CLI run (even logged out), so only the
        # credentials file proves a login.
        logged_in = (home / ".claude/.credentials.json").exists()
        self._send(200, {"ok": True, "logged_in": logged_in})

    def do_POST(self) -> None:
        if self.path != "/grade":
            return self._send(404, {"error": "not found"})
        token = os.environ.get("LLM_JUDGE_TOKEN", "")
        if not token:  # never run unauthenticated
            return self._send(503, {"error": "LLM_JUDGE_TOKEN is not configured"})
        given = self.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(413 if length > MAX_BODY else 400, {"error": "bad body size"})
        body = self.rfile.read(length)
        if not _busy.acquire(blocking=False):
            return self._send(429, {"error": "busy"})
        try:
            fields, files = parse_multipart(self.headers.get("Content-Type", ""), body)
            if not fields.get("prompt"):
                return self._send(400, {"error": "missing prompt"})
            self._send(200, grade(fields, files))
        except subprocess.TimeoutExpired:
            self._send(502, {"error": "claude timed out"})
        except Exception as exc:
            self._send(502, {"error": str(exc)[-500:]})
        finally:
            _busy.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
