"""HTTP front for `claude -p`: POST /grade (multipart), GET /health. Stdlib only."""

from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from judgelib import (
    MAX_FILE_CHARS,
    MAX_TOTAL_CHARS,
    build_prompt_with_files,
    cap_text,
    claude_argv,
    extract,
    parse_cli_output,
    safe_name,
    split_multipart,
)

DEFAULT_MAX_BODY = 210 * 1024 * 1024  # 10 files x 20 MiB from the app, plus headroom
CHUNK = 1024 * 1024
BODY_READ_TIMEOUT = 120.0
WORK_ROOT = os.environ.get("LLM_JUDGE_WORK_ROOT", "/tmp/judge")
_busy = threading.Lock()


class BadRequest(Exception):
    pass


def max_body() -> int:
    return int(os.environ.get("LLM_JUDGE_MAX_BODY") or DEFAULT_MAX_BODY)


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def logged_in() -> bool:
    # Only the credentials file proves a login; other CLI state exists when logged out.
    return (config_dir() / ".credentials.json").exists()


def stream_to_file(src, length: int, dest: Path) -> None:
    remaining = length
    with dest.open("wb") as out:
        while remaining:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                raise BadRequest("body shorter than Content-Length")
            out.write(chunk)
            remaining -= len(chunk)


def grade_request(stream, length: int, content_type: str) -> dict:
    """Stream the body to disk, split it into files, run claude, always clean up."""
    Path(WORK_ROOT).mkdir(parents=True, exist_ok=True)
    body_dir = Path(tempfile.mkdtemp(dir=WORK_ROOT))  # outside the dir claude may read
    workdir = Path(tempfile.mkdtemp(dir=WORK_ROOT))
    try:
        body = body_dir / "body"
        stream_to_file(stream, length, body)
        try:
            fields, files = split_multipart(
                body, content_type, lambda idx, fn: workdir / safe_name(fn, idx)
            )
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        body.unlink()
        if not fields.get("prompt"):
            raise BadRequest("missing prompt")
        entries: list[tuple[str, str | None, bool]] = []
        budget = MAX_TOTAL_CHARS
        for _filename, mime, path in files:
            text, offered = extract(path, mime)
            if text is not None:
                text, _ = cap_text(text, min(MAX_FILE_CHARS, budget))
                budget -= len(text)
            entries.append((path.name, text, offered))
        prompt = build_prompt_with_files(fields["prompt"], entries, workdir)
        model = fields.get("model") or os.environ.get("LLM_JUDGE_MODEL") or "opus"
        n_offered = sum(1 for e in entries if e[2])
        argv = claude_argv(model, fields.get("system", ""), workdir, n_offered > 0, n_offered)
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
        shutil.rmtree(body_dir, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    timeout = BODY_READ_TIMEOUT  # socket timeout: a stalled client cannot pin the lock

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        if code >= 400:
            self.close_connection = True  # an unread body must not be parsed as a request
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True, "logged_in": logged_in()})

    def do_POST(self) -> None:
        if self.path != "/grade":
            return self._send(404, {"error": "not found"})
        token = os.environ.get("LLM_JUDGE_TOKEN", "")
        if not token:  # never run unauthenticated
            return self._send(503, {"error": "LLM_JUDGE_TOKEN is not configured"})
        given = self.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {token}".encode()):
            return self._send(401, {"error": "unauthorized"})
        raw_len = self.headers.get("Content-Length", "")
        if not raw_len.isdigit() or int(raw_len) == 0:
            return self._send(400, {"error": "valid Content-Length required"})
        length = int(raw_len)
        if length > max_body():
            return self._send(413, {"error": "body too large"})
        # Lock before reading: a busy sidecar must not buffer another 200 MiB.
        if not _busy.acquire(blocking=False):
            return self._send(429, {"error": "busy"})
        try:
            self._send(200, grade_request(self.rfile, length, self.headers.get("Content-Type", "")))
        except BadRequest as exc:
            self._send(400, {"error": str(exc)})
        except TimeoutError:  # socket timeout while reading the body
            self._send(408, {"error": "request body timed out"})
        except subprocess.TimeoutExpired:
            self._send(502, {"error": "claude timed out"})
        except Exception as exc:
            self._send(502, {"error": str(exc)[-500:]})
        finally:
            _busy.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
