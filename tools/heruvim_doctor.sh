#!/usr/bin/env bash
set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ROOT_DIR/.env"
    set +a
fi

OPENWEBUI_URL="${HERUVIM_OPENWEBUI_BASE_URL:-http://127.0.0.1:8080}"
RAGFLOW_URL="${HERUVIM_RAGFLOW_BASE_URL:-http://127.0.0.1:9380}"
GATEWAY_URL="${HERUVIM_GATEWAY_BASE_URL:-http://127.0.0.1:9392}"
DOCX_URL="${HERUVIM_DOCX_EDITOR_BASE_URL:-http://127.0.0.1:9393}"
PDF_URL="${HERUVIM_PDF_EDITOR_BASE_URL:-http://127.0.0.1:9394}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN=python3

TMP_FILE="$(mktemp -t heruvim-doctor.XXXXXX)"
trap 'rm -f "$TMP_FILE"' EXIT
PASSED=0
FAILED=0
WARNED=0

pass() {
    printf '[OK]   %s\n' "$1"
    PASSED=$((PASSED + 1))
}

fail() {
    printf '[FAIL] %s\n' "$1"
    FAILED=$((FAILED + 1))
}

warn() {
    printf '[WARN] %s\n' "$1"
    WARNED=$((WARNED + 1))
}

check_http() {
    local label="$1"
    local url="$2"
    if curl --connect-timeout 3 --max-time 10 -fsS "$url" -o "$TMP_FILE"; then
        pass "$label"
    else
        fail "$label ($url)"
    fi
}

check_status_flag() {
    local label="$1"
    local url="$2"
    local expression="$3"
    if ! curl --connect-timeout 3 --max-time 10 -fsS "$url" -o "$TMP_FILE"; then
        fail "$label ($url)"
        return
    fi
    if "$PYTHON_BIN" -c "import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if ($expression) else 1)" "$TMP_FILE"; then
        pass "$label"
    else
        fail "$label (service answered but is not ready)"
    fi
}

printf 'HERUVIM document stack doctor\n\n'

if [[ -n "${HERUVIM_LLM_BASE_URL:-}" && -n "${HERUVIM_LLM_MODEL:-}" && -n "${HERUVIM_LLM_API_KEY:-}" ]]; then
    pass 'HERUVIM primary LLM connection is configured'
else
    fail 'HERUVIM primary LLM connection is incomplete; base URL, model and a non-empty API key are required'
fi

if [[ "${HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK:-false}" == "true" ]]; then
    warn 'RAGFlow direct chat fallback is enabled; an incomplete LLM configuration will bypass MCP routing'
else
    pass 'RAGFlow direct chat fallback is disabled'
fi

if [[ "${HERUVIM_OPENWEBUI_INTERNAL_FILE_PROCESSING:-false}" == "true" ]]; then
    warn 'Open WebUI internal file processing is enabled; current chat attachments may enter the built-in RAG path instead of MCP-only processing'
else
    pass 'Open WebUI internal file processing is disabled'
fi

if [[ "${HERUVIM_OPENWEBUI_FILE_CONTEXT:-false}" == "true" ]]; then
    warn 'Open WebUI file context is enabled; current chat attachments may trigger built-in retrieval before MCP document tools'
else
    pass 'Open WebUI file context is disabled'
fi

check_http 'Open WebUI backend' "$OPENWEBUI_URL/health"
check_http 'Gateway OpenAPI' "$GATEWAY_URL/mcp/openapi.json"
check_status_flag 'Gateway readers, RAGFlow key and OfficeCLI' "$GATEWAY_URL/tools/heruvim_tool_status" "d.get('ragflow_api_key_configured') and d.get('officecli_ready')"
check_http 'DOCX Editor OpenAPI' "$DOCX_URL/mcp/openapi.json"
check_status_flag 'DOCX Editor and OfficeCLI' "$DOCX_URL/tools/docx_status" "d.get('ok') and d.get('officecli_ready')"
check_http 'PDF Editor OpenAPI' "$PDF_URL/mcp/openapi.json"
check_status_flag 'PDF, DeepDoc OCR, OCRmyPDF and Tesseract' "$PDF_URL/tools/pdf_status" "d.get('ok') and d.get('pymupdf_ready') and d.get('ocr_ready') and d.get('ocrmypdf_ready') and d.get('tesseract_ready')"

if [[ -n "${HERUVIM_RAGFLOW_API_KEY:-}" ]]; then
    if curl --connect-timeout 3 --max-time 15 -fsS \
        -H "Authorization: Bearer $HERUVIM_RAGFLOW_API_KEY" \
        "$RAGFLOW_URL/api/v1/datasets?page=1&page_size=1" -o "$TMP_FILE" \
        && "$PYTHON_BIN" -c "import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if d.get('code') == 0 else 1)" "$TMP_FILE"; then
        pass 'RAGFlow API authentication'
    else
        fail 'RAGFlow API authentication'
    fi
else
    warn 'HERUVIM_RAGFLOW_API_KEY is not set in open-webui/.env'
fi

printf '\nResult: %s passed, %s warnings, %s failed\n' "$PASSED" "$WARNED" "$FAILED"
[[ "$FAILED" -eq 0 ]]
