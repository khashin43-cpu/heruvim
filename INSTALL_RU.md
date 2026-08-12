# ХЕРУВИМ: полная установка, dev и Docker

Инструкция описывает рабочую схему из двух приватных репозиториев:

```text
workspace/
├── heruvim/                    # интерфейс, LLM routing, память и ingestion
└── ragflow_russian_osr_mod/    # RAGFlow, русский OCR и документные MCP
```

ХЕРУВИМ не является прямым чатом RAGFlow. Основная LLM отвечает пользователю
и выбирает инструменты. Текущий файл из чата читается/редактируется MCP по
локальному пути; поиск по архиву и базе знаний выполняется через RAGFlow.

## 1. Компоненты и порты

| Компонент | Порт | Назначение |
| --- | ---: | --- |
| ХЕРУВИМ production | `3000` | пользовательский интерфейс и API |
| ХЕРУВИМ dev frontend | `5173` | Vite |
| ХЕРУВИМ dev backend | `8080` | FastAPI |
| RAGFlow | `80`, `9380` | UI и Python API |
| Gateway MCP | `9392` | RAGFlow search, чтение файлов, OfficeCLI |
| DOCX MCP | `9393` | чтение, создание и редактирование DOCX |
| PDF MCP | `9394` | PDF, OCR, замены, страницы и metadata |
| LLM endpoint | зависит от сервера | OpenAI-compatible `/v1` API |

Для ответов обязательно нужен LLM endpoint. Для поиска по базе нужны RAGFlow,
его API key и assistant/dataset. Для изменения файлов нужны MCP `9393/9394`.

## 2. Требования

- Git и GitHub CLI для приватного клонирования;
- Docker Engine/Desktop 24+ и Compose 2.26+;
- Node.js 22 и npm;
- Python 3.11 для ХЕРУВИМа;
- Python 3.13 и `uv` для текущего RAGFlow;
- минимум 16 ГБ RAM и 50 ГБ диска для полного RAGFlow;
- доступный OpenAI-compatible LLM endpoint.

Проверка:

```bash
git --version
gh auth status
docker info
docker compose version
node --version
npm --version
uv --version
```

## 3. Клонирование

Репозитории приватные, поэтому сначала выполните `gh auth login`, затем:

```bash
mkdir -p workspace
cd workspace
gh repo clone khashin43-cpu/heruvim
gh repo clone khashin43-cpu/ragflow_russian_osr_mod
```

Соседнее расположение важно для `docker-compose.mcp.yaml`. Если каталоги
расположены иначе, задайте абсолютный `RAGFLOW_PROJECT_DIR` в `heruvim/.env`.

## 4. Секреты и основной `.env`

```bash
cd workspace/heruvim
cp .env.example .env
openssl rand -hex 32
```

Результат последней команды запишите только в локальный `WEBUI_SECRET_KEY`.
Минимально заполните:

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
```

Если assistant не используется, оставьте `HERUVIM_RAGFLOW_CHAT_ID` пустым и
укажите `HERUVIM_RAGFLOW_DATASET_IDS` через запятую. Не коммитьте `.env`.

### Откуда взять RAGFlow API key

1. Откройте RAGFlow.
2. Войдите под владельцем.
3. Откройте настройки API/API Keys.
4. Создайте ключ и запишите его в локальные `.env` ХЕРУВИМа и MCP.
5. Создайте assistant, привяжите datasets и скопируйте его id.

## 5. Полный dev-запуск

### 5.1. RAGFlow

Полный source-debug и Apple Silicon/CoreML описаны в соседнем документе
`ragflow_russian_osr_mod/DEPLOY_RU.md`. Быстрый локальный вариант на Apple
Silicon:

```bash
cd workspace/ragflow_russian_osr_mod
./run_macos_m4.sh setup
./run_macos_m4.sh up
./tools/run_macos_coreml_worker.sh
```

На Linux/x86_64:

```bash
cd workspace/ragflow_russian_osr_mod/docker
docker compose -f docker-compose.yml up -d
```

Проверка: `curl -I http://127.0.0.1:9380`.

### 5.2. MCP нативно

RAGFlow `.venv` содержит общие зависимости и DeepDoc OCR:

```bash
cd workspace/ragflow_russian_osr_mod
uv sync --python 3.13 --frozen
export PYTHONPATH="$PWD"
export HERUVIM_RAGFLOW_BASE_URL='http://127.0.0.1:9380'
export HERUVIM_RAGFLOW_API_KEY='ваш-ключ'
export HERUVIM_RAGFLOW_CHAT_ID='id-assistant'
export OFFICECLI_BIN='/opt/homebrew/bin/officecli'
```

В трёх терминалах:

```bash
bash ./tools/run_heruvim_gateway_mcp.sh --transport openapi --port 9392
bash ./tools/run_heruvim_docx_editor_mcp.sh --port 9393
bash ./tools/run_heruvim_pdf_editor_mcp.sh --port 9394
```

Процессы работают в foreground. Строка `Uvicorn running` означает, что сервис
готов; это не зависание.

### 5.3. ХЕРУВИМ backend и frontend

Для нативного dev замените в `heruvim/.env` Docker-адреса:

```dotenv
HERUVIM_RAGFLOW_BASE_URL='http://127.0.0.1:9380'
HERUVIM_LLM_BASE_URL='http://127.0.0.1:11434/v1'
HERUVIM_DOCX_EDITOR_BASE_URL='http://127.0.0.1:9393'
HERUVIM_PDF_EDITOR_BASE_URL='http://127.0.0.1:9394'
CORS_ALLOW_ORIGIN='http://localhost:5173;http://127.0.0.1:5173;http://localhost:8080'
```

Установите зависимости:

```bash
cd workspace/heruvim
uv sync --python 3.11 --frozen
npm ci
```

Backend:

```bash
cd workspace/heruvim
uv run --frozen bash backend/dev.sh
```

Frontend во втором терминале:

```bash
cd workspace/heruvim
npm run dev
```

Откройте `http://127.0.0.1:5173`.

## 6. Подключение MCP в админском интерфейсе

Войдите администратором и откройте настройки подключений инструментов. Для
каждого сервиса выберите OpenAPI, включите подключение и задайте имя с
префиксом `heruvim-`.

Нативный dev:

```text
heruvim-gateway      http://127.0.0.1:9392/mcp/openapi.json
heruvim-docx-editor  http://127.0.0.1:9393/mcp/openapi.json
heruvim-pdf-editor   http://127.0.0.1:9394/mcp/openapi.json
```

Docker-контур:

```text
heruvim-gateway      http://heruvim-gateway-mcp:9392/mcp/openapi.json
heruvim-docx-editor  http://heruvim-docx-mcp:9393/mcp/openapi.json
heruvim-pdf-editor   http://heruvim-pdf-mcp:9394/mcp/openapi.json
```

Не указывайте `/openapi.json/openapi.json`. После сохранения Open WebUI должен
получить schema без `405`/`406`.

## 7. Production Docker: RAGFlow

```bash
cd workspace/ragflow_russian_osr_mod/docker
cp .env.heruvim.example .env.heruvim
```

Замените стандартные пароли в `docker/.env`. Затем:

```bash
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps
```

На Apple Silicon используйте `../run_macos_m4.sh setup` и
`../run_macos_m4.sh up`; детали и CoreML worker находятся в `DEPLOY_RU.md`.

## 8. Production Docker: ХЕРУВИМ и три MCP

Из каталога ХЕРУВИМа:

```bash
cd workspace/heruvim
mkdir -p .heruvim-documents
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  up -d --build
```

Compose собирает:

- образ ХЕРУВИМа из `Dockerfile`;
- единый документный MCP-образ из соседнего RAGFlow checkout;
- три отдельных MCP-процесса с собственными healthchecks.

ХЕРУВИМ доступен на `http://127.0.0.1:3000`. Порты MCP опубликованы только на
loopback. RAGFlow доступен MCP через `host.docker.internal:9380`.

Каталог `.heruvim-documents` монтируется только как
`/app/backend/data/uploads`. MCP не получает доступ к `webui.db`, истории
чатов или настройкам. Созданный DOCX/PDF попадает обратно в uploads, после
чего backend регистрирует его и показывает карточку с открытием/скачиванием.

### Обновление старой установки

Если у существующего контейнера документы лежат внутри старого data volume,
скопируйте их до первого запуска совместного Compose:

```bash
cd workspace/heruvim
mkdir -p .heruvim-documents
docker cp heruvim-open-webui:/app/backend/data/uploads/. ./.heruvim-documents/
```

База и история остаются в named volume `heruvim_heruvim-open-webui-data`.

## 9. OfficeCLI

Нативный режим на проверенной машине:

```bash
export OFFICECLI_BIN=/opt/homebrew/bin/officecli
/opt/homebrew/bin/officecli --version
```

Установленный `officecli 1.0.143` является macOS ARM64 Mach-O. Он не работает
в Linux Docker. Контейнерный DOCX MCP всё равно читает, создаёт и заменяет
текст через `python-docx`; статус `officecli_ready=false` в контейнере ожидаем.
Для OfficeCLI запускайте gateway/DOCX MCP нативно и подключайте Open WebUI
контейнер к `http://host.docker.internal:9392/9393`.

## 10. Проверка полного контура

```bash
curl -fsS http://127.0.0.1:9392/tools/heruvim_tool_status
curl -fsS http://127.0.0.1:9393/tools/docx_status
curl -fsS http://127.0.0.1:9394/tools/pdf_status
curl -fsS http://127.0.0.1:8080/health || true
```

Для Docker:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  ps
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  logs --tail=100 open-webui heruvim-gateway-mcp heruvim-docx-mcp heruvim-pdf-mcp
```

Проверьте в интерфейсе:

1. Прикрепите PDF и попросите изучить его: должен вызываться PDF/Gateway MCP,
   а не поиск RAGFlow по архиву.
2. Спросите «найди по документам ...»: должен вызываться RAGFlow search.
3. Создайте DOCX: в ответе должна появиться карточка, открытие и скачивание.
4. Измените PDF со сканом: используйте OCR/replace OCR и проверьте новый файл.
5. Откройте «Контур знаний»: загрузка, просмотр и удаление должны завершаться
   без `404` и `Load failed`.

Также доступен doctor:

```bash
bash ./tools/heruvim_doctor.sh
```

## 11. Остановка, обновление и резервная копия

Остановка ХЕРУВИМа без удаления данных:

```bash
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  down
```

Обновление:

```bash
git pull --ff-only
docker compose -p heruvim \
  -f docker-compose.heruvim.yaml \
  -f docker-compose.mcp.yaml \
  up -d --build
```

Сделайте резервные копии named volume ХЕРУВИМа, RAGFlow MySQL/MinIO и каталога
`.heruvim-documents`. Не используйте `docker compose down -v`, пока резервная
копия не проверена восстановлением.

После регистрации владельца установите `ENABLE_SIGNUP=false`. Для внешнего
доступа используйте HTTPS reverse proxy и не публикуйте `9392-9394`: MCP не
имеют собственной пользовательской аутентификации.

## 12. Диагностика

- `405 /mcp/openapi.json`: запущен старый server/transport; используйте текущие
  launchers и URL ровно с одним `/mcp/openapi.json`.
- `406 /mcp/api/config`: подключение добавлено как MCP вместо OpenAPI. Для трёх
  явных редакторов выбирайте OpenAPI schema URL.
- `RAGFlow API key is not configured`: ключ отсутствует в окружении процесса,
  который запустил gateway. Перезапустите процесс после изменения `.env`.
- `Generated file was not found`: Open WebUI и MCP не видят один путь. В Docker
  используйте совместный Compose и общий `.heruvim-documents`.
- `officecli_ready=false` в Docker: ожидаемое ограничение Linux-контейнера.
- OCR не готов: проверьте `pdf_status`, Tesseract `rus+eng`, OCRmyPDF и наличие
  DeepDoc в образе `heruvim-document-mcp:local`.
- Модель печатает DSML/XML: проверьте, что модель поддерживает native tool
  calling, подключения включены, а запрос отправляется через ХЕРУВИМ backend.

Расширенная архитектура и ручные API-примеры находятся в
[`HERUVIM_MCP_RUNBOOK.md`](./HERUVIM_MCP_RUNBOOK.md), а правила маршрутизации
документов в [`HERUVIM_DOCUMENT_PIPELINE.md`](./HERUVIM_DOCUMENT_PIPELINE.md).

