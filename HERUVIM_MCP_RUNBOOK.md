# ХЕРУВИМ: полный запуск компонентов

Рабочая директория этого checkout:

```text
/Users/well/heruvim_ragflow
├── open-webui   # интерфейс и backend ХЕРУВИМа
└── ragflow      # RAGFlow, OCR worker и Heruvim MCP tools
```

Правильная схема для пользователя:

```text
Open WebUI / ХЕРУВИМ
        |
        v
LLM-модель ХЕРУВИМа
        |
        +-- internal tool ragflow_search -> RAGFlow retrieval
        |
        +-- MCP server heruvim-gateway -> RAGFlow + PDF/DOCX reader + OfficeCLI
        |
        +-- optional explicit OpenAPI tools:
            heruvim-docx-editor -> DOCX create/read/edit + OfficeCLI
            heruvim-pdf-editor  -> PDF read/OCR/split/delete/rotate/merge/metadata
```

RAGFlow не должен быть отдельным пользовательским чатом. Пользователь всегда
пишет ХЕРУВИМу, а LLM сама решает, когда искать по документам или вызывать MCP.

## 0. Проверка зависимостей

```bash
docker info >/dev/null
docker compose version
/Users/well/.local/bin/uv --version
/Users/well/heruvim_ragflow/ragflow/.venv/bin/python --version
node --version
npm --version
/opt/homebrew/bin/officecli --version
```

Ожидаемо:

- Docker Desktop запущен;
- Node.js 22.x для Open WebUI;
- RAGFlow использует `/Users/well/heruvim_ragflow/ragflow/.venv`;
- OfficeCLI доступен как `/opt/homebrew/bin/officecli`.

Если Node не 22:

```bash
nvm install 22
nvm use 22
```

Если OfficeCLI не установлен:

```bash
/opt/homebrew/bin/brew install officecli
```

Шум вида `/Users/well/.zprofile:1: no such file or directory: /usr/local/bin/brew`
не относится к запуску ХЕРУВИМа. На Apple Silicon Homebrew обычно находится в
`/opt/homebrew/bin/brew`.

## 1. Подготовка Open WebUI env

Файл:

```text
/Users/well/heruvim_ragflow/open-webui/.env
```

Если файла нет:

```bash
cd /Users/well/heruvim_ragflow/open-webui
cp .env.example .env
openssl rand -hex 32
```

Минимальный `.env` для локального source-debug:

```dotenv
WEBUI_NAME='ХЕРУВИМ'
DEFAULT_LOCALE='ru-RU'
WEBUI_SECRET_KEY='ВСТАВИТЬ_РЕЗУЛЬТАТ_OPENSSL'
DATA_DIR='/Users/well/heruvim_ragflow/open-webui/.debug-data'
ENABLE_SIGNUP='true'
ENABLE_OLLAMA_API='false'
CORS_ALLOW_ORIGIN='http://localhost:5173;http://localhost:8080;http://127.0.0.1:5173;http://127.0.0.1:8080'

HERUVIM_LLM_BASE_URL='http://127.0.0.1:11434/v1'
HERUVIM_LLM_MODEL='your-openai-compatible-model'
HERUVIM_LLM_API_KEY='...'
HERUVIM_LLM_DISPLAY_NAME='ХЕРУВИМ'

HERUVIM_RAGFLOW_BASE_URL='http://127.0.0.1:9380'
HERUVIM_RAGFLOW_API_KEY='ragflow-...'
HERUVIM_RAGFLOW_CHAT_ID='ID_CHAT_ASSISTANT'
HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK='false'
HERUVIM_RAGFLOW_SYNC_ATTACHMENTS='true'
HERUVIM_OPENWEBUI_INTERNAL_FILE_PROCESSING='false'
HERUVIM_OPENWEBUI_FILE_CONTEXT='false'
HERUVIM_RAGFLOW_DATASET_IDS=''
HERUVIM_RAGFLOW_SYNC_WORKERS='2'
HERUVIM_RAGFLOW_SYNC_POLL_SECONDS='3'
HERUVIM_RAGFLOW_SYNC_TIMEOUT_SECONDS='1200'
HERUVIM_REQUIRE_SOURCE_VERIFICATION='true'
```

`HERUVIM_OPENWEBUI_INTERNAL_FILE_PROCESSING=false` означает: обычные чат-вложения
сохраняются как файлы Open WebUI, но не отправляются в локальный
`/retrieval/process/file` и не пишутся в локальную vector DB Open WebUI. Их
индексирует RAGFlow через `HERUVIM_RAGFLOW_SYNC_ATTACHMENTS=true`.

`HERUVIM_OPENWEBUI_FILE_CONTEXT=false` означает: при генерации ответа Open WebUI
не вызывает свой `query_doc` / `get_sources_from_items` по прикреплённым файлам.
Также не выдаются встроенные Open WebUI tools `query_chat_files` /
`query_knowledge_files`; документный контекст должен приходить из RAGFlow
retrieval/MCP. Это касается и PDF/DOCX, и текстовых файлов `.txt`, `.md`,
`.csv`, `.json`, `.xml`, `.log`.

Правило маршрутизации:

- текущий файл, прикреплённый прямо в чат (`посмотри документ`, `изучи док`,
  `прочитай этот PDF`) — читать через MCP tools по локальному пути вложения;
- поиск по базе, архиву, корпусу или всем загруженным документам (`найди по
  документам`, `поищи в базе`, `есть ли в архиве`) — использовать RAGFlow.

После создания первого владельца смените:

```dotenv
ENABLE_SIGNUP='false'
```

и перезапустите backend.

## 2. Подготовка зависимостей Open WebUI

Выполняется один раз или после обновления зависимостей:

```bash
cd /Users/well/heruvim_ragflow/open-webui
nvm use 22
npm ci --force
/Users/well/.local/bin/uv sync --frozen
```

## 3. Компонент 1: LLM endpoint

ХЕРУВИМ требует OpenAI-compatible LLM endpoint. Это может быть Ollama, LM
Studio, vLLM, llama.cpp server или внешний OpenAI-compatible API.

Пример для Ollama:

```bash
ollama serve
```

Проверка:

```bash
curl -sS http://127.0.0.1:11434/v1/models \
  -H "Authorization: Bearer ignored"
```

Затем в `.env` Open WebUI выставьте:

```dotenv
HERUVIM_LLM_BASE_URL='http://127.0.0.1:11434/v1'
HERUVIM_LLM_MODEL='имя-модели-из-/v1/models'
HERUVIM_LLM_API_KEY='ignored'
```

Если LLM endpoint внешний, укажите его реальный `/v1` base URL, model id и key.

## 4. Компонент 2: RAGFlow инфраструктура

Вариант быстрый, когда RAGFlow не меняется:

```bash
cd /Users/well/heruvim_ragflow/ragflow
./run_macos_m4.sh up
./run_macos_m4.sh status
```

RAGFlow UI:

```text
http://127.0.0.1/
```

RAGFlow API:

```text
http://127.0.0.1:9380
```

Проверка:

```bash
curl -I http://127.0.0.1:9380
```

Если нужно менять код RAGFlow, используйте source-debug вместо монолитного
запуска:

```bash
cd /Users/well/heruvim_ragflow/ragflow
docker compose -f docker/docker-compose-base.yml up -d
docker compose -f docker/docker-compose-base.yml ps
```

Дальше в отдельных терминалах запускаются API, worker и web UI.

## 5. Компонент 3: RAGFlow API из исходников

Нужен только для source-debug RAGFlow. Если вы запустили `./run_macos_m4.sh up`,
этот раздел пропускается.

```bash
cd /Users/well/heruvim_ragflow/ragflow
source .venv/bin/activate
export PYTHONPATH="$PWD"
export NLTK_DATA="$PWD/ragflow_deps/nltk_data"
python api/ragflow_server.py
```

Проверка:

```bash
curl -I http://127.0.0.1:9380
```

## 6. Компонент 4: RAGFlow task executor / OCR worker

Если используется source-debug:

```bash
cd /Users/well/heruvim_ragflow/ragflow
source .venv/bin/activate
export PYTHONPATH="$PWD"
export NLTK_DATA="$PWD/ragflow_deps/nltk_data"
python rag/svr/task_executor.py -i 1
```

Если используется macOS CoreML worker:

```bash
cd /Users/well/heruvim_ragflow/ragflow
./tools/run_macos_coreml_worker.sh
```

Готовность в логах:

```text
RAGFlow ingestion is ready
```

Проверка provider:

```bash
cd /Users/well/heruvim_ragflow/ragflow
grep -E 'uses CoreML|uses CPU|CoreMLExecutionProvider' \
  logs/task_executor_common_macos_coreml_0.log
```

## 7. Компонент 5: RAGFlow web UI из исходников

Нужен только для source-debug RAGFlow. Если вы используете `./run_macos_m4.sh up`,
RAGFlow UI уже доступен на `http://127.0.0.1/`.

```bash
cd /Users/well/heruvim_ragflow/ragflow/web
API_PROXY_SCHEME=python npm run dev
```

Открыть:

```text
http://127.0.0.1:9222
```

## 8. Настройка RAGFlow assistant

В RAGFlow:

1. Создайте API key.
2. Создайте chat assistant.
3. Подключите к assistant нужные datasets.
4. Скопируйте assistant id в `HERUVIM_RAGFLOW_CHAT_ID`.

Проверка доступа:

```bash
cd /Users/well/heruvim_ragflow/open-webui
set -a
source .env
set +a
curl -sS "$HERUVIM_RAGFLOW_BASE_URL/api/v1/chats/$HERUVIM_RAGFLOW_CHAT_ID" \
  -H "Authorization: Bearer $HERUVIM_RAGFLOW_API_KEY"
```

Применить профиль ХЕРУВИМа к RAGFlow assistant:

```bash
cd /Users/well/heruvim_ragflow/open-webui
set -a
source .env
set +a
/Users/well/.local/bin/uv run --frozen python scripts/configure_heruvim_ragflow.py
/Users/well/.local/bin/uv run --frozen python scripts/configure_heruvim_ragflow.py --apply
```

Первая команда dry run, вторая делает `PATCH`. Datasets и выбранная модель в
RAGFlow сохраняются.

## 9. Компонент 6: Heruvim Gateway MCP

Это основной MCP-сервер, который нужно подключать к Open WebUI. Он прячет
низкоуровневые детали и дает модели высокоуровневые tools:

- `heruvim_search_documents`;
- `heruvim_read_document`;
- `heruvim_prepare_office_document`;
- `heruvim_tool_status`.

Запуск:

```bash
cd /Users/well/heruvim_ragflow/ragflow
export HERUVIM_RAGFLOW_BASE_URL=http://127.0.0.1:9380
export HERUVIM_RAGFLOW_API_KEY=ragflow-...
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
bash ./tools/run_heruvim_gateway_mcp.sh
```

Endpoint:

```text
http://127.0.0.1:9392/mcp
```

Если Open WebUI запущен в Docker, регистрируйте URL:

```text
http://host.docker.internal:9392/mcp
```

Проверка через stdio MCP:

```bash
cd /Users/well/heruvim_ragflow/ragflow
/Users/well/heruvim_ragflow/ragflow/.venv/bin/python - <<'PY'
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="/Users/well/heruvim_ragflow/ragflow/.venv/bin/python",
        args=[
            "/Users/well/heruvim_ragflow/ragflow/mcp/heruvim_gateway/server.py",
            "--transport",
            "stdio",
        ],
        env={
            "PYTHONPATH": "/Users/well/heruvim_ragflow/ragflow",
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HERUVIM_RAGFLOW_BASE_URL": "http://127.0.0.1:9380",
            "HERUVIM_RAGFLOW_API_KEY": "ragflow-...",
            "OFFICECLI_BIN": "/opt/homebrew/bin/officecli",
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([tool.name for tool in tools.tools])
            result = await session.call_tool("heruvim_tool_status", {})
            print(result.content[0].text)

anyio.run(main)
PY
```

Ожидаемо:

```text
['heruvim_search_documents', 'heruvim_read_document', 'heruvim_prepare_office_document', 'heruvim_tool_status']
officecli_ready: true
```

## 10. Компонент 7: Document MCP / PDF-DOCX tools

Это низкоуровневый MCP-сервер для PDF/DOCX/OfficeCLI. Обычно Open WebUI должен
видеть не его напрямую, а Gateway из раздела 9. Запускайте Document MCP отдельно
только для отладки или если нужно подключить его в другой MCP client.

Важно: этот сервер работает только через `stdio`. Если запустить его руками:

```bash
bash ./tools/run_heruvim_document_mcp.sh
```

терминал будет выглядеть как зависший. Это нормально: процесс ждет MCP JSON-RPC
сообщения от клиента через stdin. Остановить его можно через `Ctrl+C`. Для
Open WebUI запускайте не Document MCP, а HTTP Gateway из раздела 9:

```bash
bash ./tools/run_heruvim_gateway_mcp.sh
```

```bash
cd /Users/well/heruvim_ragflow/ragflow
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
bash ./tools/run_heruvim_document_mcp.sh
```

Проверка через stdio:

```bash
cd /Users/well/heruvim_ragflow/ragflow
/Users/well/heruvim_ragflow/ragflow/.venv/bin/python - <<'PY'
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command="/bin/bash",
        args=["/Users/well/heruvim_ragflow/ragflow/tools/run_heruvim_document_mcp.sh"],
        env={
            "PYTHONPATH": "/Users/well/heruvim_ragflow/ragflow",
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "OFFICECLI_BIN": "/opt/homebrew/bin/officecli",
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([tool.name for tool in tools.tools])

anyio.run(main)
PY
```

## 11. Компонент 8: явный DOCX Editor OpenAPI server

Этот сервер нужен, если Open WebUI/модель плохо видит общий Gateway или если
нужно показать модели отдельные DOCX-инструменты. Он запускается отдельным
HTTP/OpenAPI процессом и дает tools:

- `heruvim_docx_status`;
- `heruvim_docx_read`;
- `heruvim_docx_create`;
- `heruvim_docx_replace_text`;
- `heruvim_officecli`.

Что реально поддержано:

- создать `.docx` из заголовка, абзацев и простых таблиц;
- прочитать текст/таблицы из `.docx`;
- заменить точные фрагменты текста в `.docx` и сохранить поверх или в новый файл;
- вызвать OfficeCLI для расширенных операций с DOCX/PPTX/XLSX.

Ограничение: `python-docx` не сохраняет весь сложный Word layout при замене
абзаца на уровне `paragraph.text`. Для юридически/дизайнерски важных DOCX лучше
сохранять результат в новый файл через `output_path` и проверять визуально.

Запуск:

```bash
cd /Users/well/heruvim_ragflow/ragflow
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
bash ./tools/run_heruvim_docx_editor_mcp.sh --port 9393
```

Проверка:

```bash
curl -sS http://127.0.0.1:9393/openapi.json
curl -sS http://127.0.0.1:9393/tools/docx_status
```

Подключение в Open WebUI:

```text
Type: OpenAPI
URL:  http://127.0.0.1:9393
Path: /openapi.json
Auth: none
Enabled: true
```

Если UI сам ходит на `/mcp/openapi.json`, можно указать:

```text
Type: OpenAPI
URL:  http://127.0.0.1:9393/mcp
Path: /openapi.json
Auth: none
Enabled: true
```

Если Open WebUI в Docker:

```text
URL:  http://host.docker.internal:9393
Path: /openapi.json
Auth: none
Enabled: true
```

Пример ручного вызова создания DOCX:

```bash
curl -sS http://127.0.0.1:9393/tools/docx_create \
  -H 'Content-Type: application/json' \
  -d '{
    "output_path": "/Users/well/heruvim_ragflow/out/test.docx",
    "title": "Тестовый документ",
    "paragraphs": ["Первый абзац.", "Второй абзац."],
    "tables": [{"rows": [["Задача", "Статус"], ["Проверка", "Готово"]]}]
  }'
```

Пример ручной замены текста:

```bash
curl -sS http://127.0.0.1:9393/tools/docx_replace_text \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/out/test.docx",
    "output_path": "/Users/well/heruvim_ragflow/out/test-edited.docx",
    "replacements": [{"find": "Первый абзац.", "replace": "Обновленный первый абзац."}]
  }'
```

## 12. Компонент 9: явный PDF Editor + OCR OpenAPI server

Этот сервер нужен для PDF-инструментов отдельно от Gateway. Он дает tools:

- `heruvim_pdf_status`;
- `heruvim_pdf_read`;
- `heruvim_pdf_ocr`;
- `heruvim_pdf_extract_text_blocks`;
- `heruvim_pdf_replace_text`;
- `heruvim_pdf_replace_ocr_text`;
- `heruvim_pdf_redact_text`;
- `heruvim_pdf_add_text`;
- `heruvim_pdf_make_searchable`;
- `heruvim_pdf_extract_pages`;
- `heruvim_pdf_delete_pages`;
- `heruvim_pdf_rotate_pages`;
- `heruvim_pdf_merge`;
- `heruvim_pdf_set_metadata`.

Что реально поддержано:

- извлечь нативный текст через `pdfplumber`;
- если текста мало или явно указан `ocr: true`, запустить OCR через
  `deepdoc.vision.ocr.OCR` из RAGFlow;
- извлечь текстовые блоки и координаты через PyMuPDF;
- заменить точный текст в born-digital PDF через redaction + вставку нового
  текста;
- удалить текст из PDF через redaction;
- добавить текст в заданный прямоугольник страницы;
- сделать scanned PDF searchable через OCRmyPDF/Tesseract;
- заменить визуальный текст на скане: DeepDoc OCR ищет bbox, сервер закрывает
  старую область и вставляет новый текст поверх;
- извлечь страницы в новый PDF;
- удалить страницы;
- повернуть страницы;
- объединить несколько PDF;
- изменить metadata.

Ограничение: PDF не хранит документ как Word. Короткие замены, даты, суммы,
ФИО, реквизиты, штампы, подписи, комментарии и OCR-замены на сканах работают
практично. Длинная правка абзаца с автоматическим переносом и сохранением всей
верстки надежнее делается через PDF -> DOCX -> редактирование -> экспорт PDF.

Дополнительные зависимости для полноценного PDF editing/OCR:

```bash
cd /Users/well/heruvim_ragflow/ragflow
/Users/well/.local/bin/uv pip install --python .venv/bin/python \
  pymupdf pytesseract ocrmypdf pikepdf

/opt/homebrew/bin/brew install tesseract tesseract-lang qpdf
```

Запуск:

```bash
cd /Users/well/heruvim_ragflow/ragflow
export PYTHONPATH="$PWD"
bash ./tools/run_heruvim_pdf_editor_mcp.sh --port 9394
```

Проверка:

```bash
curl -sS http://127.0.0.1:9394/openapi.json
curl -sS http://127.0.0.1:9394/tools/pdf_status
```

Если `ocr_ready: false`, обычное чтение PDF и page-операции всё равно работают,
но OCR не готов. Тогда проверьте, что сервер запущен именно из RAGFlow `.venv`:

```bash
cd /Users/well/heruvim_ragflow/ragflow
source .venv/bin/activate
python - <<'PY'
from deepdoc.vision.ocr import OCR
print(OCR)
PY
```

Подключение в Open WebUI:

```text
Type: OpenAPI
URL:  http://127.0.0.1:9394
Path: /openapi.json
Auth: none
Enabled: true
```

Если UI сам ходит на `/mcp/openapi.json`, можно указать:

```text
Type: OpenAPI
URL:  http://127.0.0.1:9394/mcp
Path: /openapi.json
Auth: none
Enabled: true
```

Если Open WebUI в Docker:

```text
URL:  http://host.docker.internal:9394
Path: /openapi.json
Auth: none
Enabled: true
```

Пример чтения с OCR fallback:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_read \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/sample.pdf",
    "max_pages": 3,
    "ocr_if_empty": true
  }'
```

Пример OCR конкретных страниц:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_ocr \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/scanned.pdf",
    "pages": "1-2"
  }'
```

Пример сделать scanned PDF searchable:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_make_searchable \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/scanned.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/scanned-searchable.pdf",
    "language": "rus+eng",
    "deskew": true,
    "optimize": 1
  }'
```

Пример заменить текст в обычном текстовом PDF:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_replace_text \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/contract.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/contract-edited.pdf",
    "find": "100 000",
    "replace": "120 000",
    "pages": "1-3",
    "fontsize": 10
  }'
```

Пример заменить визуальный текст на скане:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_replace_ocr_text \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/scanned.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/scanned-edited.pdf",
    "find": "Иванов",
    "replace": "Петров",
    "pages": "1",
    "fontsize": 9,
    "padding": 2
  }'
```

Пример добавить текст по координатам:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_add_text \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/form.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/form-filled.pdf",
    "page": 1,
    "rect": [100, 140, 260, 170],
    "text": "Согласовано",
    "fontsize": 12
  }'
```

Пример удаления страниц:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_delete_pages \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/source.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/source-without-page-2.pdf",
    "pages": "2"
  }'
```

Пример поворота страниц:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_rotate_pages \
  -H 'Content-Type: application/json' \
  -d '{
    "path": "/Users/well/heruvim_ragflow/in/source.pdf",
    "output_path": "/Users/well/heruvim_ragflow/out/source-rotated.pdf",
    "pages": "1,3",
    "degrees": 90
  }'
```

Пример объединения PDF:

```bash
curl -sS http://127.0.0.1:9394/tools/pdf_merge \
  -H 'Content-Type: application/json' \
  -d '{
    "paths": [
      "/Users/well/heruvim_ragflow/in/a.pdf",
      "/Users/well/heruvim_ragflow/in/b.pdf"
    ],
    "output_path": "/Users/well/heruvim_ragflow/out/merged.pdf"
  }'
```

### Если нужен внешний PDF MCP сильнее локального

Для отдельного MCP-клиента можно рассмотреть:

- `pdf-mcp`: Python MCP server с chunked reading, hybrid search, OCR через
  Tesseract, таблицами, картинками и SQLite-кэшем;
- `@sylphx/pdf-reader-mcp`: локальный Rust/Node PDF reader MCP с OCR,
  таблицами, structured evidence и page-level citations.

Для Open WebUI в этом checkout всё равно практичнее использовать наш
`heruvim-pdf-editor`, потому что он отдает OpenAPI напрямую и не требует
дополнительного STDIO bridge.

## 13. Компонент 10: OfficeCLI

OfficeCLI не является сервером. Это локальная CLI-зависимость, которую вызывает
Gateway или Document MCP.

Проверки:

```bash
/opt/homebrew/bin/officecli --version
/opt/homebrew/bin/officecli --help
```

Env для MCP:

```bash
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
export PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

OfficeCLI используется для:

- создания DOCX/PPTX/XLSX;
- редактирования офисных документов;
- подготовки справок, поручений, презентаций и таблиц;
- экспорта, если это поддержано текущей OfficeCLI-командой.

## 14. Компонент 11: Open WebUI backend

Перед запуском backend должны уже работать те OpenAPI tool servers, которые
должны быть видны модели:

```bash
curl -sS http://127.0.0.1:9392/openapi.json >/dev/null
curl -sS http://127.0.0.1:9393/openapi.json >/dev/null
curl -sS http://127.0.0.1:9394/openapi.json >/dev/null
```

Если backend стартует, когда tool server недоступен, Open WebUI может не
подтянуть его tools до перезапуска или ручного сохранения настроек в UI.

```bash
cd /Users/well/heruvim_ragflow/open-webui
/Users/well/.local/bin/uv run --frozen bash backend/dev.sh
```

Backend:

```text
http://127.0.0.1:8080
```

Проверка:

```bash
curl http://127.0.0.1:8080/health
```

Backend запускает:

- Open WebUI API;
- Heruvim RAGFlow ingestion queue;
- internal LLM tool `ragflow_search`;
- подключение MCP tool servers из `TOOL_SERVER_CONNECTIONS` или настроек UI.

## 15. Компонент 12: Open WebUI frontend

```bash
cd /Users/well/heruvim_ragflow/open-webui
nvm use 22
node_modules/.bin/vite dev --host --force
```

Открыть:

```text
http://localhost:5173
```

Используйте именно `localhost`, если браузер раньше открывал другие Open WebUI
на `127.0.0.1:5173`: так меньше риска получить старый Service Worker.

## 16. Подключение Gateway и отдельных редакторов в Open WebUI

Рекомендуемый путь для текущего UI Open WebUI: зарегистрировать Gateway как
OpenAPI tool server.

Запуск Gateway остается тем же:

```bash
cd /Users/well/heruvim_ragflow/ragflow
export HERUVIM_RAGFLOW_BASE_URL=http://127.0.0.1:9380
export HERUVIM_RAGFLOW_API_KEY=ragflow-...
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
bash ./tools/run_heruvim_gateway_mcp.sh
```

Gateway отдает OpenAPI здесь:

```text
http://127.0.0.1:9392/openapi.json
```

И совместимый путь для случая, когда UI добавляет `/openapi.json` к `/mcp`:

```text
http://127.0.0.1:9392/mcp/openapi.json
```

В интерфейсе Open WebUI:

```text
Type: OpenAPI
URL:  http://127.0.0.1:9392
Path: /openapi.json
Auth: none
Enabled: true
```

Если Open WebUI запущен в Docker:

```text
URL:  http://host.docker.internal:9392
Path: /openapi.json
Auth: none
Enabled: true
```

Проверка:

```bash
curl -sS http://127.0.0.1:9392/openapi.json
curl -sS http://127.0.0.1:9392/tools/heruvim_tool_status
```

Через `.env` Open WebUI для OpenAPI:

```dotenv
TOOL_SERVER_CONNECTIONS='[
  {
    "id": "heruvim-gateway",
    "name": "ХЕРУВИМ Gateway",
    "url": "http://127.0.0.1:9392",
    "path": "/openapi.json",
    "type": "openapi",
    "auth_type": "none",
    "key": "",
    "headers": {},
    "config": {"enable": true},
    "info": {"id": "heruvim-gateway"}
  },
  {
    "id": "heruvim-docx-editor",
    "name": "ХЕРУВИМ DOCX Editor",
    "url": "http://127.0.0.1:9393",
    "path": "/openapi.json",
    "type": "openapi",
    "auth_type": "none",
    "key": "",
    "headers": {},
    "config": {"enable": true},
    "info": {"id": "heruvim-docx-editor"}
  },
  {
    "id": "heruvim-pdf-editor",
    "name": "ХЕРУВИМ PDF Editor OCR",
    "url": "http://127.0.0.1:9394",
    "path": "/openapi.json",
    "type": "openapi",
    "auth_type": "none",
    "key": "",
    "headers": {},
    "config": {"enable": true},
    "info": {"id": "heruvim-pdf-editor"}
  }
]'
```

Если UI не сохраняет tool servers, можно записать их прямо в локальную debug DB.
Команда не содержит секретов:

```bash
sqlite3 /Users/well/heruvim_ragflow/open-webui/.debug-data/webui.db "
insert into config(key,value,updated_at)
values(
  'tool_server.connections',
  json('[
    {
      \"id\":\"heruvim-gateway\",
      \"name\":\"ХЕРУВИМ Gateway\",
      \"url\":\"http://127.0.0.1:9392\",
      \"path\":\"/openapi.json\",
      \"type\":\"openapi\",
      \"auth_type\":\"none\",
      \"key\":\"\",
      \"headers\":{},
      \"config\":{\"enable\":true},
      \"info\":{\"id\":\"heruvim-gateway\",\"name\":\"ХЕРУВИМ Gateway\"}
    },
    {
      \"id\":\"heruvim-docx-editor\",
      \"name\":\"ХЕРУВИМ DOCX Editor\",
      \"url\":\"http://127.0.0.1:9393\",
      \"path\":\"/openapi.json\",
      \"type\":\"openapi\",
      \"auth_type\":\"none\",
      \"key\":\"\",
      \"headers\":{},
      \"config\":{\"enable\":true},
      \"info\":{\"id\":\"heruvim-docx-editor\",\"name\":\"ХЕРУВИМ DOCX Editor\"}
    },
    {
      \"id\":\"heruvim-pdf-editor\",
      \"name\":\"ХЕРУВИМ PDF Editor OCR\",
      \"url\":\"http://127.0.0.1:9394\",
      \"path\":\"/openapi.json\",
      \"type\":\"openapi\",
      \"auth_type\":\"none\",
      \"key\":\"\",
      \"headers\":{},
      \"config\":{\"enable\":true},
      \"info\":{\"id\":\"heruvim-pdf-editor\",\"name\":\"ХЕРУВИМ PDF Editor OCR\"}
    }
  ]'),
  strftime('%s','now')
)
on conflict(key) do update
set value=excluded.value, updated_at=excluded.updated_at;
"
```

После прямой записи в DB обязательно перезапустите Open WebUI backend.

В этом checkout backend также автоматически подмешивает enabled OpenAPI
tool servers с id/name `heruvim-*` или `ХЕРУВИМ*` в обычные чаты, даже если UI
не передал `tool_ids`. Это нужно, чтобы модель не отвечала “у меня нет PDF/DOCX
инструментов” после успешного подключения серверов. После изменения списка
серверов или кода tool server всё равно перезапускайте backend.

### MCP-вариант

Готовый пример:

```text
/Users/well/heruvim_ragflow/ragflow/mcp/heruvim_gateway/openwebui_mcp_config.json
```

Используйте его только если UI явно дает выбрать `type: mcp`. Через `.env`
Open WebUI:

```dotenv
TOOL_SERVER_CONNECTIONS='[
  {
    "id": "heruvim-gateway",
    "name": "ХЕРУВИМ Gateway",
    "url": "http://127.0.0.1:9392/mcp",
    "type": "mcp",
    "auth_type": "none",
    "key": "",
    "headers": {},
    "config": {"enable": true},
    "info": {"id": "heruvim-gateway"}
  }
]'
```

Если Open WebUI в Docker:

```json
"url": "http://host.docker.internal:9392/mcp"
```

Через API администратора:

```bash
curl -sS http://127.0.0.1:8080/api/config/tool_servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "TOOL_SERVER_CONNECTIONS": [
      {
        "id": "heruvim-gateway",
        "name": "ХЕРУВИМ Gateway",
        "url": "http://127.0.0.1:9392/mcp",
        "type": "mcp",
        "auth_type": "none",
        "key": "",
        "headers": {},
        "config": {"enable": true},
        "info": {"id": "heruvim-gateway"}
      }
    ]
  }'
```

Проверка регистрации:

```bash
curl -sS http://127.0.0.1:8080/api/config/tool_servers/verify \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "heruvim-gateway",
    "name": "ХЕРУВИМ Gateway",
    "url": "http://127.0.0.1:9392/mcp",
    "type": "mcp",
    "auth_type": "none",
    "key": "",
    "headers": {},
    "config": {"enable": true},
    "info": {"id": "heruvim-gateway"}
  }'
```

Успешный ответ содержит `status: true` и список tools.

## 17. Production Docker запуск Open WebUI

Для полного контура клонируйте `heruvim` и `ragflow_russian_osr_mod` рядом.
Сначала запустите RAGFlow по `ragflow_russian_osr_mod/DEPLOY_RU.md`, затем
ХЕРУВИМ вместе с тремя MCP:

```bash
cd heruvim
mkdir -p .heruvim-documents
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  up -d --build
```

Open WebUI:

```text
http://localhost:3000
```

Логи:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  logs -f open-webui heruvim-gateway-mcp heruvim-docx-mcp heruvim-pdf-mcp
```

В Docker-режиме для host-сервисов используйте:

```dotenv
HERUVIM_LLM_BASE_URL='http://host.docker.internal:11434/v1'
HERUVIM_RAGFLOW_BASE_URL='http://host.docker.internal:9380'
```

В админском интерфейсе используйте внутренние OpenAPI URL:

```text
http://heruvim-gateway-mcp:9392/mcp/openapi.json
http://heruvim-docx-mcp:9393/mcp/openapi.json
http://heruvim-pdf-mcp:9394/mcp/openapi.json
```

MCP получают доступ только к общему каталогу uploads
`.heruvim-documents`, а не ко всей базе Open WebUI. Полная Docker-инструкция,
миграция старых uploads и ограничения OfficeCLI находятся в `INSTALL_RU.md`.

## 18. Рекомендуемый порядок запуска по терминалам

Полный локальный source/debug контур:

1. LLM endpoint.
2. RAGFlow infrastructure:
   `docker compose -f docker/docker-compose-base.yml up -d`.
3. RAGFlow API:
   `python api/ragflow_server.py`.
4. RAGFlow worker:
   `python rag/svr/task_executor.py -i 1` или `./tools/run_macos_coreml_worker.sh`.
5. RAGFlow web UI:
   `API_PROXY_SCHEME=python npm run dev`.
6. Heruvim Gateway MCP:
   `bash ./tools/run_heruvim_gateway_mcp.sh`.
7. DOCX Editor OpenAPI:
   `bash ./tools/run_heruvim_docx_editor_mcp.sh --port 9393`.
8. PDF Editor OCR OpenAPI:
   `bash ./tools/run_heruvim_pdf_editor_mcp.sh --port 9394`.
9. Document MCP, если отлаживается отдельно:
   `bash ./tools/run_heruvim_document_mcp.sh`.
10. Open WebUI backend:
   `/Users/well/.local/bin/uv run --frozen bash backend/dev.sh`.
11. Open WebUI frontend:
   `node_modules/.bin/vite dev --host --force`.

Быстрый локальный контур:

1. LLM endpoint.
2. `cd /Users/well/heruvim_ragflow/ragflow && ./run_macos_m4.sh up`.
3. `cd /Users/well/heruvim_ragflow/ragflow && ./tools/run_macos_coreml_worker.sh`.
4. Heruvim Gateway MCP.
5. DOCX Editor OpenAPI, если нужны явные DOCX tools.
6. PDF Editor OCR OpenAPI, если нужны явные PDF tools.
7. Open WebUI backend.
8. Open WebUI frontend.

## 19. Проверка полного контура

Базовые health checks:

```bash
curl -I http://localhost:5173
curl http://127.0.0.1:8080/health
curl -I http://127.0.0.1:9380
curl -sS http://127.0.0.1:9392/tools/heruvim_tool_status
curl -sS http://127.0.0.1:9393/tools/docx_status
curl -sS http://127.0.0.1:9394/tools/pdf_status
/opt/homebrew/bin/officecli --version
```

RAGFlow assistant:

```bash
cd /Users/well/heruvim_ragflow/open-webui
set -a
source .env
set +a
curl -sS "$HERUVIM_RAGFLOW_BASE_URL/api/v1/chats/$HERUVIM_RAGFLOW_CHAT_ID" \
  -H "Authorization: Bearer $HERUVIM_RAGFLOW_API_KEY"
```

Open WebUI ingestion:

```text
http://localhost:5173/ingestion
```

UI-сценарий:

1. Загрузить PDF/DOCX в чат.
2. Открыть `/ingestion`.
3. Дождаться `ready` и ненулевого числа chunks.
4. Спросить: `Какие риски в этом документе?`
5. Проверить, что ответ идет от ХЕРУВИМа, а не от отдельного RAGFlow-чата.
6. Попросить подготовить DOCX-справку и проверить вызов OfficeCLI через MCP.
7. Попросить отредактировать DOCX: модель должна вызвать `heruvim_docx_replace_text`
   или `heruvim_officecli`.
8. Попросить обработать PDF: модель должна вызвать `heruvim_pdf_read`,
   `heruvim_pdf_ocr` или page-operation tool.

## 20. Остановка

Процессы в терминалах:

```text
Ctrl+C
```

RAGFlow Docker быстрый режим:

```bash
cd /Users/well/heruvim_ragflow/ragflow
./run_macos_m4.sh down
```

RAGFlow source-debug infrastructure:

```bash
cd /Users/well/heruvim_ragflow/ragflow
docker compose -f docker/docker-compose-base.yml down
```

Open WebUI Docker:

```bash
cd heruvim
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  down
```

Эти команды не удаляют volumes.

## 21. Что не надо делать

- Не регистрируйте RAGFlow как единственную пользовательскую chat-модель, если
  настроена LLM. `HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK` должен быть `false`.
- Не подключайте в Open WebUI много низкоуровневых Office/PDF tools напрямую,
  если работает Gateway. Модель должна видеть высокоуровневые Heruvim tools.
- Не храните `HERUVIM_RAGFLOW_API_KEY`, `HERUVIM_LLM_API_KEY` и
  `WEBUI_SECRET_KEY` в git.
- Не запускайте Open WebUI на Node 26; используйте Node 22.
