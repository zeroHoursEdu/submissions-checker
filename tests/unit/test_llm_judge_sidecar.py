"""Pure helpers of the llm-judge sidecar (docker/llm-judge/judgelib.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker" / "llm-judge"))

from judgelib import (  # noqa: E402
    build_prompt_with_files,
    claude_argv,
    extract,
    parse_cli_output,
    pdf_needs_visual,
    safe_name,
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


def test_argv_no_read_when_not_needed(tmp_path):
    assert "--allowedTools" not in claude_argv("opus", "S", tmp_path, False)


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
    s = build_prompt_with_files("P", [("01_s.pdf", "tiny", True), ("02_i.png", None, True)])
    assert "Read tool" in s and "/" in s.split("Read tool")[1]
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


def _post_grade(monkeypatch, token, auth, tmp_path):
    """Drive the real handler over a socket; claude itself is stubbed."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    import server

    monkeypatch.setattr(server, "WORK_ROOT", str(tmp_path))
    if token is None:
        monkeypatch.delenv("LLM_JUDGE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("LLM_JUDGE_TOKEN", token)
    monkeypatch.setattr(
        server,
        "grade",
        lambda fields, files: {"result": fields["prompt"], "model": "m", "usage": {}},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = (
            b'--B\r\nContent-Disposition: form-data; name="prompt"\r\n\r\nhello\r\n'
            b'--B\r\nContent-Disposition: form-data; name="files"; filename="a.py"\r\n'
            b"Content-Type: text/x-python\r\n\r\nprint(1)\r\n--B--\r\n"
        )
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port)
        headers = {"Content-Type": "multipart/form-data; boundary=B"}
        if auth:
            headers["Authorization"] = auth
        conn.request("POST", "/grade", body, headers)
        r = conn.getresponse()
        return r.status, json.loads(r.read())
    finally:
        httpd.shutdown()


def test_server_refuses_when_token_unset(monkeypatch, tmp_path):
    assert _post_grade(monkeypatch, None, "Bearer x", tmp_path)[0] == 503


def test_server_rejects_bad_token(monkeypatch, tmp_path):
    assert _post_grade(monkeypatch, "secret", "Bearer nope", tmp_path)[0] == 401
    assert _post_grade(monkeypatch, "secret", None, tmp_path)[0] == 401


def test_server_accepts_good_token_and_parses_multipart(monkeypatch, tmp_path):
    status, body = _post_grade(monkeypatch, "secret", "Bearer secret", tmp_path)
    assert status == 200 and body["result"] == "hello"


def test_grade_pipes_prompt_with_safe_names_and_cleans_up(monkeypatch, tmp_path):
    import server

    stub = tmp_path / "bin"
    stub.mkdir()
    claude = stub / "claude"
    claude.write_text(
        "#!/bin/sh\nPROMPT=$(cat)\n"
        'printf \'{"result": %s, "modelUsage": {"m1": {}}, "usage": {}}\' '
        '"$(printf %s "$PROMPT" | python3 -c \'import json,sys;print(json.dumps(sys.stdin.read()))\')"\n'
    )
    claude.chmod(0o755)
    work = tmp_path / "work"
    monkeypatch.setattr(server, "WORK_ROOT", str(work))
    monkeypatch.setenv("PATH", f"{stub}:/usr/bin:/bin")
    out = server.grade(
        {"prompt": "PROMPT-PREFIX", "system": "S"},
        [("../../evil name.py", "text/x-python", b"print('hi')")],
    )
    assert out["result"].startswith("PROMPT-PREFIX")
    assert "--- FILE: 01_evil_name.py ---" in out["result"] and "evil name" not in out["result"]
    assert out["model"] == "m1"
    assert list(work.iterdir()) == []  # workdir removed
