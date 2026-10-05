"""Pure helpers of the llm-judge sidecar (docker/llm-judge/judgelib.py)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker" / "llm-judge"))

from judgelib import (  # noqa: E402
    build_prompt_with_files,
    cap_text,
    claude_argv,
    extract,
    parse_cli_output,
    pdf_needs_visual,
    safe_name,
    split_multipart,
)


def test_safe_name_strips_path():
    assert safe_name("../../etc/passwd", 1) == "01_passwd"


def test_safe_name_replaces_odd_characters_and_truncates():
    out = safe_name("звіт лаби 1.pdf", 3)
    assert out.startswith("03_") and " " not in out
    assert len(safe_name("a" * 300, 1)) == len("01_") + 100


def test_pdf_needs_visual():
    assert pdf_needs_visual(["a" * 300, "x"])
    assert not pdf_needs_visual(["a" * 300])


def test_argv_restricts_tools(tmp_path):
    a = claude_argv("opus", "SYS", tmp_path, allow_read=True)
    assert a[:2] == ["claude", "-p"]
    assert "Read" in a[a.index("--allowedTools") + 1]
    assert "Bash" in a[a.index("--disallowedTools") + 1]
    assert a[a.index("--model") + 1] == "opus"
    assert a[a.index("--append-system-prompt") + 1] == "SYS"
    assert a[a.index("--add-dir") + 1] == str(tmp_path)
    # allow-list (not just deny-list): only Read exists, and nothing ambient is loaded
    assert a[a.index("--tools") + 1] == "Read"
    assert a[a.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a


def test_argv_no_read_when_not_needed(tmp_path):
    a = claude_argv("opus", "S", tmp_path, False)
    assert "--allowedTools" not in a
    assert a[a.index("--tools") + 1] == ""  # no tools at all


def test_argv_max_turns_scales_with_offered_files(tmp_path):
    def turns(n):
        a = claude_argv("opus", "S", tmp_path, n > 0, n)
        return int(a[a.index("--max-turns") + 1])

    assert turns(0) == 6 and turns(2) == 6
    assert turns(10) == 12


def test_extract_text_file(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)")
    assert extract(p, "text/x-python") == ("print(1)", False)


def test_extract_image_offered(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"\x89PNG")
    assert extract(p, "image/png") == (None, True)


def test_extract_unknown_is_unsupported(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"\x00\x01")
    assert extract(p, "application/octet-stream") == (None, False)


def test_prompt_lists_files_after_unchanged_prefix():
    s = build_prompt_with_files("P", [("01_a.py", "x", False), ("02_b.bin", None, False)])
    assert s.startswith("P")
    assert "--- FILE: 01_a.py ---" in s and "unsupported" in s


def test_prompt_offers_read_pointer():
    root = Path("/tmp/judge/xyz")
    s = build_prompt_with_files("P", [("01_s.pdf", "tiny", True), ("02_i.png", None, True)], root)
    assert f"Read tool: {root / '01_s.pdf'}]" in s
    assert f"Read tool: {root / '02_i.png'}]" in s
    assert "[no text layer]" in s


def test_parse_cli_output():
    out = parse_cli_output(
        json.dumps({"type": "result", "result": "{}", "usage": {"input_tokens": 1}})
    )
    assert out["result"] == "{}" and out["usage"] == {"input_tokens": 1}


def test_parse_cli_output_model_from_model_usage():
    raw = json.dumps({"result": "x", "modelUsage": {"claude-opus-4": {}}})
    assert parse_cli_output(raw)["model"] == "claude-opus-4"


def test_parse_cli_output_rejects_error_result():
    with pytest.raises(ValueError):
        parse_cli_output(json.dumps({"type": "result", "is_error": True, "result": "boom"}))


def test_parse_cli_output_rejects_garbage():
    with pytest.raises(ValueError):
        parse_cli_output("not json")


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler not installed")
def test_extract_pdf():
    path = Path(__file__).resolve().parents[1] / "fixtures" / "sample.pdf"
    text, offer = extract(path, "application/pdf")
    assert text is not None and "Hello sidecar" in text
    assert offer  # one short page -> offered for visual reading
    assert subprocess.run(["pdftotext", str(path), "-"], capture_output=True).returncode == 0


def _multipart(parts, boundary="B"):
    out = b""
    for name, filename, ctype, data in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"--{boundary}\r\nContent-Disposition: {disp}\r\n".encode()
        if ctype:
            out += f"Content-Type: {ctype}\r\n".encode()
        out += b"\r\n" + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode()


CT = "multipart/form-data; boundary=B"


def test_split_multipart_is_byte_exact_for_multi_mb_binary(tmp_path):
    blob = os.urandom(6 * 1024 * 1024) + b"\r\n--Bx\r\n"  # looks like a delimiter, is not one
    body = _multipart(
        [
            ("system", None, None, "сис".encode()),
            ("prompt", None, None, b"hello"),
            ("files", "a b.bin", "application/octet-stream", blob),
            ("files", "../c.txt", "text/plain", b""),
        ]
    )
    src = tmp_path / "body"
    src.write_bytes(body)
    fields, files = split_multipart(src, CT, lambda i, fn: tmp_path / f"f{i}")
    assert fields == {"system": "сис", "prompt": "hello"}
    assert [(f[0], f[1]) for f in files] == [
        ("a b.bin", "application/octet-stream"),
        ("../c.txt", "text/plain"),
    ]
    assert files[0][2].read_bytes() == blob and files[1][2].read_bytes() == b""


def test_split_multipart_does_not_load_the_body(tmp_path):
    import tracemalloc

    src = tmp_path / "body"
    src.write_bytes(
        _multipart([("prompt", None, None, b"p"), ("files", "big", "x/y", b"z" * 50_000_000)])
    )
    tracemalloc.start()
    split_multipart(src, CT, lambda i, fn: tmp_path / "out")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024  # a bytes blob of the body would be >= 50 MB


@pytest.mark.parametrize("body", [b"", b"garbage", b"--B\r\nContent-Disposition: x\r\n"])
def test_split_multipart_rejects_malformed(tmp_path, body):
    src = tmp_path / "body"
    src.write_bytes(body)
    with pytest.raises(ValueError):
        split_multipart(src, CT, lambda i, fn: tmp_path / "o")


def test_cap_text_marks_truncation():
    assert cap_text("abc", 10) == ("abc", False)
    out, cut = cap_text("a" * 50, 10)
    assert cut and out.startswith("a" * 10) and out.endswith("[truncated]")


def test_extract_pdf_timeout_means_offer_for_read(tmp_path, monkeypatch):
    import judgelib

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("pdftotext", 1)

    monkeypatch.setattr(judgelib.subprocess, "run", boom)
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF")
    assert extract(p, "application/pdf") == (None, True)


# --- server -----------------------------------------------------------------------------


@pytest.fixture
def srv(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("LLM_JUDGE_TOKEN", "secret")
    return server


def _http(server, body, headers=None, auth="Bearer secret", length=None):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        h = {"Content-Type": CT}
        if auth:
            h["Authorization"] = auth
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
        conn.putrequest("POST", "/grade")
        for k, v in h.items():
            conn.putheader(k, v)
        if length is not False:
            conn.putheader("Content-Length", str(len(body) if length is None else length))
        conn.endheaders(body)
        r = conn.getresponse()
        return r.status, json.loads(r.read())
    finally:
        httpd.shutdown()


GOOD = _multipart(
    [("prompt", None, None, b"hello"), ("files", "a.py", "text/x-python", b"print(1)")]
)


def test_server_refuses_when_token_unset(srv, monkeypatch):
    monkeypatch.delenv("LLM_JUDGE_TOKEN")
    assert _http(srv, GOOD)[0] == 503


def test_server_rejects_bad_token(srv):
    assert _http(srv, GOOD, auth="Bearer nope")[0] == 401
    assert _http(srv, GOOD, auth=None)[0] == 401


def test_server_accepts_good_token_and_parses_multipart(srv, monkeypatch):
    seen = {}

    def fake(stream, length, ctype):
        seen["body"] = stream.read(length)
        return {"result": "ok", "model": "m", "usage": {}}

    monkeypatch.setattr(srv, "grade_request", fake)
    status, body = _http(srv, GOOD)
    assert (status, body["result"]) == (200, "ok") and seen["body"] == GOOD


def test_server_413_over_cap_without_reading(srv, monkeypatch):
    monkeypatch.setenv("LLM_JUDGE_MAX_BODY", "100")
    monkeypatch.setattr(srv, "grade_request", lambda *a: pytest.fail("must not read"))
    assert _http(srv, b"x" * 1000)[0] == 413


@pytest.mark.parametrize("bad", ["abc", "-5", "0"])
def test_server_400_on_bad_content_length(srv, bad):
    assert _http(srv, b"", headers={"Content-Length": bad}, length=False)[0] == 400


def test_server_400_on_missing_content_length(srv):
    assert _http(srv, b"", length=False)[0] == 400


def test_server_429_when_busy_without_reading_body(srv, monkeypatch):
    monkeypatch.setattr(srv, "grade_request", lambda *a: pytest.fail("must not read"))
    assert srv._busy.acquire(blocking=False)
    try:
        assert _http(srv, GOOD)[0] == 429
    finally:
        srv._busy.release()


def test_server_400_on_malformed_multipart(srv):
    assert _http(srv, b"not multipart at all")[0] == 400


class _ChunkSpy(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.max_read = 0

    def read(self, n=-1):
        self.max_read = max(self.max_read, n)
        return super().read(n)


def _stub_claude(tmp_path, monkeypatch, script):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    claude = stub / "claude"
    claude.write_text("#!/bin/sh\n" + script)
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub}:/usr/bin:/bin")


ECHO = (
    "PROMPT=$(cat)\n"
    'printf \'{"result": %s, "modelUsage": {"m1": {}}, "usage": {}}\' '
    '"$(printf %s "$PROMPT" | python3 -c \'import json,sys;print(json.dumps(sys.stdin.read()))\')"\n'
)


def _work_left(srv):
    root = Path(srv.WORK_ROOT)
    return list(root.iterdir()) if root.exists() else []


def test_grade_request_streams_pipes_prompt_with_safe_names_and_cleans_up(
    srv, tmp_path, monkeypatch
):
    _stub_claude(tmp_path, monkeypatch, ECHO)
    body = _multipart(
        [
            ("system", None, None, b"S"),
            ("prompt", None, None, b"PROMPT-PREFIX"),
            ("files", "../../evil name.py", "text/x-python", b"print('hi')"),
        ]
    )
    stream = _ChunkSpy(body)
    out = srv.grade_request(stream, len(body), CT)
    assert stream.max_read <= 1024 * 1024  # chunked, never a single read of the body
    assert out["result"].startswith("PROMPT-PREFIX")
    assert "--- FILE: 01_evil_name.py ---" in out["result"] and "evil name" not in out["result"]
    assert out["model"] == "m1"
    assert _work_left(srv) == []


def test_grade_request_truncates_text(srv, tmp_path, monkeypatch):
    _stub_claude(tmp_path, monkeypatch, ECHO)
    big = b"a" * 200_000
    body = _multipart(
        [("prompt", None, None, b"P")]
        + [("files", f"f{i}.txt", "text/plain", big) for i in range(4)]
    )
    out = srv.grade_request(io.BytesIO(body), len(body), CT)["result"]
    assert out.count("[truncated]") == 4  # per-file cap hit; total cap hit by the 3rd/4th
    assert len(out) < 400_000 + 5_000


def test_workdir_removed_when_claude_fails(srv, tmp_path, monkeypatch):
    _stub_claude(tmp_path, monkeypatch, "cat >/dev/null; echo boom >&2; exit 3\n")
    with pytest.raises(RuntimeError):
        srv.grade_request(io.BytesIO(GOOD), len(GOOD), CT)
    assert _work_left(srv) == []


def test_workdir_removed_on_timeout(srv, tmp_path, monkeypatch):
    _stub_claude(tmp_path, monkeypatch, "exec sleep 5\n")
    monkeypatch.setenv("LLM_JUDGE_CLI_TIMEOUT", "0.3")
    with pytest.raises(subprocess.TimeoutExpired):
        srv.grade_request(io.BytesIO(GOOD), len(GOOD), CT)
    assert _work_left(srv) == []


def test_health_reads_claude_config_dir(srv, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert srv.logged_in() is False
    (cfg / ".credentials.json").write_text("{}")
    assert srv.logged_in() is True
