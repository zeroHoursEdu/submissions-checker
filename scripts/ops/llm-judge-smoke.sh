#!/usr/bin/env bash
# Smoke-test a running llm-judge sidecar: GET /health, then POST a sample PDF to /grade.
set -euo pipefail

usage() {
  cat <<'EOF2'
Usage: llm-judge-smoke.sh [--url URL] [--file PATH]

  --url URL    sidecar base URL (default: $LLM_JUDGE_URL or http://localhost:8090)
  --file PATH  file to send (default: tests/fixtures/sample.pdf)

Needs LLM_JUDGE_TOKEN in the environment (same value the sidecar was started with).
Prints /health, then the /grade response: 200 with {"result","model","usage"} when the
sidecar is logged in; 502 "Not logged in" until you run `claude` -> /login inside it.
Not destructive; one real model call when logged in.
EOF2
}

url="${LLM_JUDGE_URL:-http://localhost:8090}"
file="$(dirname "$0")/../../tests/fixtures/sample.pdf"
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --url) url="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
: "${LLM_JUDGE_TOKEN:?set LLM_JUDGE_TOKEN}"
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 2; }

echo "== GET $url/health"
curl -fsS "$url/health"; echo
echo "== POST $url/grade ($file)"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$url/grade" \
  -H "Authorization: Bearer $LLM_JUDGE_TOKEN" \
  -F 'system=You grade tiny documents.' \
  -F 'prompt=Reply with a JSON object {"summary": "<one sentence about the attached file>"}.' \
  -F 'model=opus' \
  -F "files=@$file;type=application/pdf"
