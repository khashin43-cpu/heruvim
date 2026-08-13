# ХЕРУВИМ

ХЕРУВИМ — документный AI-ассистент на базе Open WebUI. Основная LLM ведёт
диалог и сама выбирает инструмент:

- прикреплённые в текущий чат PDF, DOCX и TXT обрабатываются документными MCP;
- поиск по архиву и базе знаний выполняется через RAGFlow;
- созданные DOCX/PDF показываются в чате и доступны для скачивания;
- обычные вопросы не отправляются напрямую в RAGFlow.

Репозиторий RAGFlow, русского OCR и MCP:
[`khashin43-cpu/ragflow_russian_osr_mod`](https://github.com/khashin43-cpu/ragflow_russian_osr_mod).

## Быстрый запуск в Docker

### 1. Требования

- Docker Engine/Desktop 24+;
- Docker Compose 2.26+;
- GitHub CLI с доступом к приватным репозиториям;
- не менее 16 ГБ RAM и 50 ГБ свободного места для полного контура;
- OpenAI-compatible LLM endpoint.

Проверьте окружение:

```bash
gh auth status
docker info
docker compose version
```

### 2. Клонирование

Оба репозитория должны находиться рядом:

```bash
mkdir -p workspace
cd workspace
gh repo clone khashin43-cpu/heruvim
gh repo clone khashin43-cpu/ragflow_russian_osr_mod
```

Итоговая структура:

```text
workspace/
├── heruvim/
└── ragflow_russian_osr_mod/
```

### 3. Запуск RAGFlow

Linux/x86_64:

```bash
cd workspace/ragflow_russian_osr_mod/docker
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
```

macOS на Apple Silicon:

```bash
cd workspace/ragflow_russian_osr_mod
./run_macos_m4.sh setup
./run_macos_m4.sh up
```

Для ускорения OCR/layout через CoreML запустите в отдельном терминале:

```bash
cd workspace/ragflow_russian_osr_mod
./tools/run_macos_coreml_worker.sh
```

RAGFlow UI будет доступен на `http://127.0.0.1`, API — на
`http://127.0.0.1:9380`.

### 4. Настройка RAGFlow

1. Откройте RAGFlow и создайте пользователя-владельца.
2. Добавьте embedding-модель и при необходимости chat-модель.
3. Создайте dataset и загрузите документы.
4. Создайте assistant и подключите к нему datasets.
5. Создайте API key в настройках RAGFlow.
6. Сохраните API key и assistant id для `.env` ХЕРУВИМа.

### 5. Настройка ХЕРУВИМа

```bash
cd workspace/heruvim
cp .env.example .env
mkdir -p .heruvim-documents
openssl rand -hex 32
```

Запишите сгенерированное значение и параметры сервисов в `.env`:

```dotenv
WEBUI_SECRET_KEY='случайное-значение'

HERUVIM_LLM_BASE_URL='http://host.docker.internal:11434/v1'
HERUVIM_LLM_MODEL='точный-id-модели'
HERUVIM_LLM_API_KEY='ключ-или-локальное-значение'

HERUVIM_RAGFLOW_BASE_URL='http://host.docker.internal:9380'
HERUVIM_RAGFLOW_API_KEY='ragflow-api-key'
HERUVIM_RAGFLOW_CHAT_ID='id-assistant'
HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK='false'

HERUVIM_DOCUMENTS_DIR='./.heruvim-documents'
ENABLE_SIGNUP='true'
```

Если assistant не используется, оставьте `HERUVIM_RAGFLOW_CHAT_ID` пустым и
задайте `HERUVIM_RAGFLOW_DATASET_IDS` через запятую.

Для LM Studio, Ollama, vLLM или другого локального сервера укажите его
OpenAI-compatible `/v1` URL и точный model id.

### 6. Запуск ХЕРУВИМа и MCP

```bash
cd workspace/heruvim
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  up -d --build
```

Откройте `http://127.0.0.1:3000` и зарегистрируйте владельца. После этого
установите в `.env`:

```dotenv
ENABLE_SIGNUP='false'
```

и примените изменение:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  up -d
```

### 7. Подключение MCP в админской панели

В админских настройках подключений инструментов добавьте три подключения типа
OpenAPI и включите их:

```text
heruvim-gateway      http://heruvim-gateway-mcp:9392/mcp/openapi.json
heruvim-docx-editor  http://heruvim-docx-mcp:9393/mcp/openapi.json
heruvim-pdf-editor   http://heruvim-pdf-mcp:9394/mcp/openapi.json
```

Не добавляйте повторный `/openapi.json` и не выбирайте MCP transport для этих
трёх URL: они предоставляют OpenAPI schema.

### 8. Проверка

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  ps

curl -fsS http://127.0.0.1:9392/tools/heruvim_tool_status
curl -fsS http://127.0.0.1:9393/tools/docx_status
curl -fsS http://127.0.0.1:9394/tools/pdf_status
```

Проверьте четыре пользовательских сценария:

1. Прикрепите PDF в чат и попросите изучить его — должен использоваться MCP,
   а не поиск по RAGFlow.
2. Спросите «найди по документам...» — должен использоваться RAGFlow.
3. Попросите создать DOCX — файл должен появиться в чате с открытием и
   скачиванием.
4. Попросите изменить PDF или скан — должен появиться новый PDF-файл.

## Запуск для разработки

### 1. Зависимости ХЕРУВИМа

Требуются Node.js 22, npm, Python 3.11 и `uv`:

```bash
cd workspace/heruvim
cp .env.example .env
uv sync --python 3.11 --frozen
npm ci
```

Для нативного запуска измените в `.env` адреса Docker на loopback:

```dotenv
HERUVIM_RAGFLOW_BASE_URL='http://127.0.0.1:9380'
HERUVIM_LLM_BASE_URL='http://127.0.0.1:11434/v1'
HERUVIM_DOCX_EDITOR_BASE_URL='http://127.0.0.1:9393'
HERUVIM_PDF_EDITOR_BASE_URL='http://127.0.0.1:9394'
CORS_ALLOW_ORIGIN='http://localhost:5173;http://127.0.0.1:5173;http://localhost:8080'
```

### 2. Документные MCP

В репозитории RAGFlow:

```bash
cd workspace/ragflow_russian_osr_mod
uv sync --python 3.13 --frozen
export PYTHONPATH="$PWD"
export HERUVIM_RAGFLOW_BASE_URL='http://127.0.0.1:9380'
export HERUVIM_RAGFLOW_API_KEY='ваш-ключ'
export HERUVIM_RAGFLOW_CHAT_ID='id-assistant'
```

Запустите в трёх терминалах:

```bash
bash ./tools/run_heruvim_gateway_mcp.sh --transport openapi --port 9392
bash ./tools/run_heruvim_docx_editor_mcp.sh --port 9393
bash ./tools/run_heruvim_pdf_editor_mcp.sh --port 9394
```

### 3. Backend и frontend

Backend:

```bash
cd workspace/heruvim
uv run --frozen bash backend/dev.sh
```

Frontend в другом терминале:

```bash
cd workspace/heruvim
npm run dev
```

Откройте `http://127.0.0.1:5173`. Для нативного режима OpenAPI URL в админской
панели используют `http://127.0.0.1:9392-9394/mcp/openapi.json`.

## OfficeCLI

На проверенной macOS-машине:

```bash
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
/opt/homebrew/bin/officecli --version
```

Проверенный `officecli 1.0.143` является macOS ARM64 binary и не выполняется в
Linux Docker. Контейнерный DOCX MCP без него продолжает читать, создавать и
редактировать DOCX через `python-docx`. Для полного OfficeCLI запускайте
Gateway/DOCX MCP нативно.

## Управление

Логи:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  logs -f open-webui heruvim-gateway-mcp heruvim-docx-mcp heruvim-pdf-mcp
```

Остановка без удаления данных:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  down
```

Не используйте `down -v`, если не хотите удалить persistent volumes. Делайте
резервные копии volume ХЕРУВИМа, RAGFlow MySQL/MinIO и каталога
`.heruvim-documents`.

## Подробная документация

- [INSTALL_RU.md](./INSTALL_RU.md) — полный runbook и диагностика;
- [HERUVIM_DOCUMENT_PIPELINE.md](./HERUVIM_DOCUMENT_PIPELINE.md) — правила
  маршрутизации текущих файлов и RAGFlow;
- [HERUVIM_MCP_RUNBOOK.md](./HERUVIM_MCP_RUNBOOK.md) — операции и API MCP;
- [HERUVIM.md](./HERUVIM.md) — продуктовая архитектура.

Проект основан на Open WebUI и сохраняет соответствующие upstream-лицензии в
репозитории.
