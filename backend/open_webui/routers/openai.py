from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse
from uuid import uuid4

import aiofiles
import aiohttp
from aiocache import cached
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from open_webui.config import (
    CACHE_DIR,
    HERUVIM_LLM_API_KEY,
    HERUVIM_LLM_BASE_URL,
    HERUVIM_LLM_DISPLAY_NAME,
    HERUVIM_LLM_ENABLED,
    HERUVIM_LLM_MODEL,
    HERUVIM_RAGFLOW_API_KEY,
    HERUVIM_RAGFLOW_AUTO_RETRIEVAL,
    HERUVIM_RAGFLOW_AUTO_RETRIEVAL_MODE,
    HERUVIM_RAGFLOW_BASE_URL,
    HERUVIM_RAGFLOW_CHAT_ID,
    HERUVIM_RAGFLOW_DATASET_IDS,
    HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK,
    HERUVIM_RAGFLOW_ENABLED,
    HERUVIM_RAGFLOW_FULL_DOCUMENT_LIMIT,
    HERUVIM_RAGFLOW_FULL_DOCUMENT_MAX_CHARS,
    HERUVIM_RAGFLOW_FULL_DOCUMENTS,
    HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE,
    HERUVIM_RAGFLOW_RETRIEVAL_SIMILARITY_THRESHOLD,
    HERUVIM_RAGFLOW_RETRIEVAL_VECTOR_WEIGHT,
    HERUVIM_REQUIRE_SOURCE_VERIFICATION,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.events import EVENTS, publish_event, publish_model_provider_request_failed
from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    BYPASS_MODEL_ACCESS_CONTROL,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    ENABLE_OPENAI_API_PASSTHROUGH,
    FORWARD_SESSION_INFO_HEADER_CHAT_ID,
    MODELS_CACHE_TTL,
)
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.files import FileForm, Files
from open_webui.models.groups import Groups
from open_webui.models.models import Models
from open_webui.models.users import UserModel
from open_webui.storage.provider import Storage
from open_webui.utils.access_control import check_model_access, has_connection_access, has_permission
from open_webui.utils.anthropic import get_anthropic_models, is_anthropic_url
from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.headers import get_custom_headers, include_user_info_headers
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.model_ids import strip_provider_model_prefix
from open_webui.utils.misc import (
    convert_logit_bias_input_to_json,
    stream_chunks_handler,
)
from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_system_prompt_to_body,
)
from open_webui.utils.session_pool import (
    cleanup_response,
    get_client_timeout,
    get_session,
    stream_wrapper,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


##########################################
#
# Utility functions
# Let the responses returned through this gate be worth
# the question that summoned them.
#
##########################################

# Headers that become stale after aiohttp auto-decompresses the upstream
# response body.  Forwarding them verbatim causes desktop / programmatic
# clients to attempt decompression of an already-decoded payload, resulting
# in ZlibError.  See https://github.com/aio-libs/aiohttp/issues/4462.
_STRIP_PROXY_HEADERS = frozenset({'Content-Encoding', 'Content-Length', 'Transfer-Encoding'})

_HERUVIM_DOCUMENT_QUERY_RE = re.compile(
    r'(?i)\b('
    r'документ|документа|документы|файл|pdf|скан|источник|источники|'
    r'договор|контракт|акт|сч[её]т|приказ|письмо|протокол|'
    r'найди|найти|поищи|поиск|где\s+.*упомина|упоминается|'
    r'фамили[яи]|инн|огрн|снилс|паспорт|адрес|сумм[ауеы]|дата|срок'
    r')\b'
)

_HERUVIM_RAGFLOW_TOOL_NAME = 'ragflow_search'
_HERUVIM_READ_DOCUMENT_TOOL_NAME = 'heruvim_read_document'
_HERUVIM_READ_DOCUMENT_TOOL_ALIASES = {
    _HERUVIM_READ_DOCUMENT_TOOL_NAME,
    'read_document',
    'read_pdf',
    'heruvim_read_pdf',
    'heruvim_pdf_read',
    'heruvim_extract_pdf_text',
    'heruvim_get_document_text',
    'extract_pdf_text',
}
_HERUVIM_LOCAL_TOOL_ALIASES = {
    'create_docx': 'heruvim_docx_create',
    'docx_create': 'heruvim_docx_create',
    'create_word_document': 'heruvim_docx_create',
    'read_docx': 'heruvim_docx_read',
    'docx_read': 'heruvim_docx_read',
    'replace_docx_text': 'heruvim_docx_replace_text',
    'edit_docx': 'heruvim_docx_replace_text',
    'read_pdf': 'heruvim_pdf_read',
    'pdf_read': 'heruvim_pdf_read',
    'ocr_pdf': 'heruvim_pdf_ocr',
    'make_pdf_searchable': 'heruvim_pdf_make_searchable',
    'replace_pdf_text': 'heruvim_pdf_replace_text',
    'merge_pdf': 'heruvim_pdf_merge',
}
_HERUVIM_DOCUMENT_EDIT_QUERY_RE = re.compile(
    r'(?i)\b(редакт|измени|изменить|замени|заменить|удали|удалить|добавь|добавить|'
    r'поверни|повернуть|объедини|объединить|склей|вырежи|извлеки\s+страниц|'
    r'создай|создать|сохрани|сохранить|метаданн|ocr|распознай|сделай\s+поисковым)\w*\b'
)
_HERUVIM_INDEXED_CORPUS_QUERY_RE = re.compile(
    r'(?i)\b(ragflow|баз[ае]\s+знаний|архив[еу]?|корпус[еу]?|по\s+документам|'
    r'во\s+всех\s+документах|среди\s+документов)\b'
)
_HERUVIM_DOCX_EDITOR_BASE_URL = os.getenv('HERUVIM_DOCX_EDITOR_BASE_URL', 'http://127.0.0.1:9393').rstrip('/')
_HERUVIM_PDF_EDITOR_BASE_URL = os.getenv('HERUVIM_PDF_EDITOR_BASE_URL', 'http://127.0.0.1:9394').rstrip('/')
_HERUVIM_LOCAL_OPENAPI_TOOLS = {
    'heruvim_docx_health': ('GET', _HERUVIM_DOCX_EDITOR_BASE_URL, '/health'),
    'heruvim_docx_status': ('GET', _HERUVIM_DOCX_EDITOR_BASE_URL, '/tools/docx_status'),
    'heruvim_docx_read': ('POST', _HERUVIM_DOCX_EDITOR_BASE_URL, '/tools/docx_read'),
    'heruvim_docx_create': ('POST', _HERUVIM_DOCX_EDITOR_BASE_URL, '/tools/docx_create'),
    'heruvim_docx_replace_text': ('POST', _HERUVIM_DOCX_EDITOR_BASE_URL, '/tools/docx_replace_text'),
    'heruvim_officecli': ('POST', _HERUVIM_DOCX_EDITOR_BASE_URL, '/tools/officecli'),
    'heruvim_pdf_health': ('GET', _HERUVIM_PDF_EDITOR_BASE_URL, '/health'),
    'heruvim_pdf_status': ('GET', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_status'),
    'heruvim_pdf_read': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_read'),
    'heruvim_pdf_ocr': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_ocr'),
    'heruvim_pdf_extract_text_blocks': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_extract_text_blocks'),
    'heruvim_pdf_replace_text': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_replace_text'),
    'heruvim_pdf_replace_ocr_text': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_replace_ocr_text'),
    'heruvim_pdf_redact_text': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_redact_text'),
    'heruvim_pdf_add_text': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_add_text'),
    'heruvim_pdf_make_searchable': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_make_searchable'),
    'heruvim_pdf_extract_pages': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_extract_pages'),
    'heruvim_pdf_delete_pages': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_delete_pages'),
    'heruvim_pdf_rotate_pages': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_rotate_pages'),
    'heruvim_pdf_merge': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_merge'),
    'heruvim_pdf_set_metadata': ('POST', _HERUVIM_PDF_EDITOR_BASE_URL, '/tools/pdf_set_metadata'),
}
_HERUVIM_ARTIFACT_TOOLS = {
    'heruvim_docx_create',
    'heruvim_docx_replace_text',
    'heruvim_pdf_replace_text',
    'heruvim_pdf_replace_ocr_text',
    'heruvim_pdf_redact_text',
    'heruvim_pdf_add_text',
    'heruvim_pdf_make_searchable',
    'heruvim_pdf_extract_pages',
    'heruvim_pdf_delete_pages',
    'heruvim_pdf_rotate_pages',
    'heruvim_pdf_merge',
    'heruvim_pdf_set_metadata',
}
_HERUVIM_RAGFLOW_TOOL_SYSTEM_PROMPT = (
    'You are ХЕРУВИМ. Answer normally from your own knowledge when indexed documents are not needed. '
    'Current chat attachments and indexed RAGFlow corpora are different sources. '
    'When the user asks to inspect, read, summarize, quote, or edit a file attached to the current chat, use the '
    'provided local attachment path with the available MCP document tools; do not search RAGFlow for that current attachment. '
    'When the user asks to find, search, compare, verify, or answer across indexed documents, knowledge base, archive, corpus, '
    'or documents not attached to the current chat, call '
    f'{_HERUVIM_RAGFLOW_TOOL_NAME}. Do not claim that indexed documents are unavailable before calling the tool. '
    'If the user says "вытащи документ", "достань документ", "покажи документ", or "прочитай документ" and a current '
    'chat attachment is listed, read that local attachment through MCP. If no current attachment is listed, search RAGFlow. '
    'If the user asks to display the document in chat, paste the available document text or the largest available '
    'source excerpt directly in the chat, with the document name/id first. Do not answer with offers or capability lists. '
    'When preview_url or download_url is present, include Markdown links named "Открыть документ" and "Скачать документ". '
    'When a generated artifact is available, do not expose its local output_path; show the file card and links instead. '
    'For DOCX creation use the exact heruvim_docx_create operation; never print create_docx or DSML markup. '
    'Never print XML, DSML, JSON, or any internal tool-call markup to the user. '
    'After receiving tool results, answer using the evidence and include document names, ids, pages, or snippets '
    'when they are available. If the tool returns no hits, say that no indexed document evidence was found.'
)

_HERUVIM_RAGFLOW_TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': _HERUVIM_RAGFLOW_TOOL_NAME,
        'description': (
            'Search indexed user documents in RAGFlow. Use this for questions about uploaded files, PDFs, DOCX, '
            'scans, reports, contracts, document contents, or any answer that needs source-backed evidence.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'The focused document-search query derived from the user request.',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'Maximum number of chunks to return.',
                    'minimum': 1,
                    'maximum': 30,
                    'default': HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE,
                },
            },
            'required': ['query'],
        },
    },
}

_HERUVIM_READ_DOCUMENT_TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': _HERUVIM_READ_DOCUMENT_TOOL_NAME,
        'description': (
            'Read a local file attached to the current chat. Use this for "посмотри документ", "изучи док", '
            '"прочитай файл", summaries, quotes, or inspection of the current PDF/DOCX/TXT attachment.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Absolute local_path from HERUVIM_CURRENT_CHAT_ATTACHMENTS.',
                },
                'max_pages': {
                    'type': 'integer',
                    'default': 20,
                    'minimum': 1,
                    'maximum': 500,
                },
                'max_chars_per_page': {
                    'type': 'integer',
                    'default': 12000,
                    'minimum': 1000,
                    'maximum': 50000,
                },
            },
            'required': ['path'],
        },
    },
}


async def _heruvim_public_base_url(request: Request) -> str:
    configured_url = str(
        os.getenv('HERUVIM_PUBLIC_BASE_URL') or await Config.get('webui.url') or ''
    ).strip().rstrip('/')
    if configured_url:
        parsed = urlparse(configured_url)
        if parsed.hostname in {'localhost', '127.0.0.1'} and parsed.port == 5173:
            return parsed._replace(netloc=f'{parsed.hostname}:8080').geturl().rstrip('/')
        return configured_url

    base_url = str(request.base_url).rstrip('/')
    parsed = urlparse(base_url)
    if parsed.hostname in {'localhost', '127.0.0.1'} and parsed.port == 5173:
        return parsed._replace(netloc=f'{parsed.hostname}:8080').geturl().rstrip('/')
    if request.url.path == '/api/v1/automations/internal' and parsed.hostname in {'localhost', '127.0.0.1'}:
        return os.getenv('HERUVIM_OPENWEBUI_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')
    return base_url


def _heruvim_document_url(document_id: str, public_base_url: str = '', *, download: bool = False) -> str:
    path = f'/api/v1/heruvim/ragflow/documents/{document_id}/preview'
    if download:
        path = f'{path}?download=1'
    return f'{public_base_url.rstrip("/")}{path}' if public_base_url else path


def _metadata_file_ids(metadata: dict | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    ids = []
    for item in metadata.get('files') or []:
        if not isinstance(item, dict):
            continue
        candidates = [
            item.get('id'),
            item.get('url'),
            (item.get('file') or {}).get('id') if isinstance(item.get('file'), dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and not candidate.startswith(('http://', 'https://', 'data:')):
                ids.append(candidate)
                break
    return list(dict.fromkeys(ids))


def _message_text(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            str(item.get('text') or item.get('content') or '')
            for item in content
            if isinstance(item, dict)
        )
    return ''


def _first_current_attachment_path(payload: dict) -> str:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return ''
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _message_text(message)
        if 'HERUVIM_CURRENT_CHAT_ATTACHMENTS' not in text:
            continue
        match = re.search(r'local_path="([^"]+)"', text)
        if match:
            return match.group(1).strip()
    return ''


async def _heruvim_attached_file_context(metadata: dict | None, user: UserModel) -> str:
    file_ids = _metadata_file_ids(metadata)
    if not file_ids:
        return ''

    lines = [
        'HERUVIM_CURRENT_CHAT_ATTACHMENTS:',
        'These files are attached to the current chat. They are not RAGFlow search results.',
        'For requests like "посмотри документ", "изучи док", "прочитай файл", "сделай summary", or edits of these files, use MCP document tools with the local_path below.',
        'Use RAGFlow only when the user asks to search the indexed knowledge base/corpus/archive or documents not attached to this chat.',
        '',
    ]
    readable_count = 0
    for file_id in file_ids[:10]:
        file = await Files.get_file_by_id(file_id)
        if not file or (user.role != 'admin' and file.user_id != user.id):
            continue
        local_path = ''
        if file.path:
            try:
                local_path = await asyncio.to_thread(Storage.get_file, file.path)
            except Exception:
                local_path = file.path
        meta = file.meta or {}
        content_type = meta.get('content_type') or ''
        size = meta.get('size') or ''
        suffix = ''
        if file.filename and '.' in file.filename:
            suffix = file.filename.rsplit('.', 1)[-1].lower()
        tool_hint = 'heruvim_read_document'
        if suffix == 'pdf':
            tool_hint = 'heruvim_pdf_read or heruvim_read_document'
        elif suffix == 'docx':
            tool_hint = 'heruvim_docx_read or heruvim_read_document'
        elif suffix in {'txt', 'md', 'csv', 'json', 'jsonl', 'xml', 'yaml', 'yml', 'log', 'rtf'}:
            tool_hint = 'heruvim_read_document'
        lines.append(
            f'file_id="{file.id}" name="{file.filename}" content_type="{content_type}" size="{size}" '
            f'local_path="{local_path}" preferred_tool="{tool_hint}"'
        )
        readable_count += 1

    if not readable_count:
        return ''
    return '\n'.join(lines)


def _extract_latest_user_text(messages: list | None) -> str:
    if not isinstance(messages, list):
        return ''
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text') or item.get('content')
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
            return '\n'.join(parts).strip()
    return ''


def _add_heruvim_ragflow_tool(payload: dict) -> None:
    tools = payload.get('tools')
    if not isinstance(tools, list):
        tools = []
    existing_names = {
        (tool.get('function') or {}).get('name')
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get('function'), dict)
    }
    if _HERUVIM_RAGFLOW_TOOL_NAME not in existing_names:
        tools.append(_HERUVIM_RAGFLOW_TOOL_SCHEMA)
    if _HERUVIM_READ_DOCUMENT_TOOL_NAME not in existing_names:
        tools.append(_HERUVIM_READ_DOCUMENT_TOOL_SCHEMA)
    payload['tools'] = tools

    if payload.get('tool_choice') in (None, 'none'):
        payload['tool_choice'] = 'auto'


def _add_heruvim_ragflow_tool_prompt(payload: dict) -> None:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return

    system_message = next(
        (message for message in messages if isinstance(message, dict) and message.get('role') in {'system', 'developer'}),
        None,
    )
    if system_message and isinstance(system_message.get('content'), str):
        if _HERUVIM_RAGFLOW_TOOL_SYSTEM_PROMPT not in system_message['content']:
            system_message['content'] = f"{system_message['content']}\n\n{_HERUVIM_RAGFLOW_TOOL_SYSTEM_PROMPT}"
    else:
        messages.insert(0, {'role': 'system', 'content': _HERUVIM_RAGFLOW_TOOL_SYSTEM_PROMPT})


def _prepare_heruvim_tool_result_payload(payload: dict, context: str) -> dict:
    final_payload = copy.deepcopy(payload)
    messages = final_payload.get('messages')
    if not isinstance(messages, list):
        return payload

    # A failed pseudo-call from an earlier turn is internal protocol noise. If
    # it remains in history, models that use DSML tend to imitate it instead of
    # answering from the tool result that is already available.
    final_payload['messages'] = [
        message
        for message in messages
        if not (
            isinstance(message, dict)
            and message.get('role') == 'assistant'
            and isinstance(message.get('content'), str)
            and ('DSML' in message['content'] or ('tool_calls' in message['content'] and 'invoke name=' in message['content']))
        )
    ]
    for message in final_payload['messages']:
        if not isinstance(message, dict) or message.get('role') not in {'system', 'developer'}:
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        content = content.replace(_HERUVIM_RAGFLOW_TOOL_SYSTEM_PROMPT, '')
        content = re.sub(
            r'Heruvim document tools are available\..*?Do not claim these tools are unavailable\.',
            '',
            content,
            flags=re.DOTALL,
        )
        content = re.sub(
            r'HERUVIM_CURRENT_CHAT_ATTACHMENTS:.*?(?=\n\n|\Z)',
            '',
            content,
            flags=re.DOTALL,
        )
        message['content'] = content.strip()
    _inject_system_context(
        final_payload,
        context
        + '\n\nHERUVIM_TOOL_EXECUTION_COMPLETE: The required document tool has already run. '
        'Use its result above and answer the user now. Do not call another tool, discuss tool names, '
        'or emit any internal protocol markup.',
    )
    final_payload.pop('tools', None)
    final_payload.pop('tool_choice', None)
    return final_payload


def _extract_chat_completion_tool_calls(response: Any) -> tuple[dict | None, list[dict]]:
    if not isinstance(response, dict):
        return None, []
    choices = response.get('choices')
    if not isinstance(choices, list) or not choices:
        return None, []
    message = choices[0].get('message') if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None, []
    tool_calls = message.get('tool_calls')
    if not isinstance(tool_calls, list):
        return message, []
    return message, [tool_call for tool_call in tool_calls if isinstance(tool_call, dict)]


def _normalize_heruvim_tool_name(name: str) -> str:
    if name in _HERUVIM_READ_DOCUMENT_TOOL_ALIASES:
        return _HERUVIM_READ_DOCUMENT_TOOL_NAME
    return _HERUVIM_LOCAL_TOOL_ALIASES.get(name, name)


def _normalize_heruvim_tool_arguments(name: str, arguments: dict) -> dict:
    normalized = dict(arguments)
    if name == 'heruvim_docx_create':
        normalized['output_path'] = normalized.get('output_path') or normalized.get('path')
        content = normalized.pop('content', None)
        if isinstance(content, str) and content.strip() and not normalized.get('paragraphs'):
            blocks = [block.strip() for block in re.split(r'\n\s*\n', content) if block.strip()]
            if not normalized.get('title') and blocks:
                first_line = blocks[0].splitlines()[0].strip()
                if len(first_line) <= 200:
                    normalized['title'] = first_line
                    blocks[0] = '\n'.join(blocks[0].splitlines()[1:]).strip()
                    blocks = [block for block in blocks if block]
            normalized['paragraphs'] = blocks
        normalized.pop('path', None)
    elif name == 'heruvim_docx_replace_text' and not normalized.get('replacements'):
        find = normalized.pop('find', normalized.pop('old_text', None))
        replace = normalized.pop('replace', normalized.pop('new_text', None))
        if isinstance(find, str) and isinstance(replace, str):
            normalized['replacements'] = [{'find': find, 'replace': replace}]
    return normalized


def _extract_dsml_heruvim_tool_calls(message: dict | None) -> list[dict]:
    if not isinstance(message, dict):
        return []
    content = message.get('content')
    if isinstance(content, list):
        text = '\n'.join(
            str(item.get('text') or item.get('content') or '')
            for item in content
            if isinstance(item, dict)
        )
    elif isinstance(content, str):
        text = content
    else:
        return []

    tool_names = {
        _HERUVIM_RAGFLOW_TOOL_NAME,
        *_HERUVIM_READ_DOCUMENT_TOOL_ALIASES,
        *_HERUVIM_LOCAL_OPENAPI_TOOLS,
        *_HERUVIM_LOCAL_TOOL_ALIASES,
    }
    if not any(name in text for name in tool_names) or 'invoke' not in text:
        return []

    calls = []
    invoke_re = re.compile(r'<[^>]*invoke\s+name=["\'](?P<tool>[^"\']+)["\'][^>]*>(?P<body>.*?)</[^>]*invoke>', re.DOTALL)
    parameter_re = re.compile(
        r'<[^>]*parameter\s+name=["\'](?P<name>[^"\']+)["\'][^>]*>(?P<value>.*?)</[^>]*parameter>',
        re.DOTALL,
    )
    for match in invoke_re.finditer(text):
        tool_name = match.group('tool').strip()
        if tool_name not in tool_names:
            continue
        normalized_tool_name = _normalize_heruvim_tool_name(tool_name)
        args = {}
        body = match.group('body')
        for parameter in parameter_re.finditer(body):
            name = parameter.group('name').strip()
            value = re.sub(r'<[^>]+>', '', parameter.group('value')).strip()
            if name:
                try:
                    args[name] = json.loads(value)
                except Exception:
                    args[name] = value
        if normalized_tool_name == _HERUVIM_RAGFLOW_TOOL_NAME:
            query = str(args.get('query') or '').strip()
            if not query:
                continue
            arguments = {'query': query}
        elif normalized_tool_name == _HERUVIM_READ_DOCUMENT_TOOL_NAME:
            path = str(args.get('path') or '').strip()
            if not path:
                continue
            arguments = {'path': path}
            for key in ('max_pages', 'max_chars_per_page'):
                if args.get(key):
                    arguments[key] = args[key]
        else:
            arguments = _normalize_heruvim_tool_arguments(normalized_tool_name, args)
        calls.append(
            {
                'id': f'heruvim_dsml_{len(calls) + 1}',
                'type': 'function',
                'function': {
                    'name': normalized_tool_name,
                    'arguments': json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    return calls


def _synthesize_current_attachment_tool_call(payload: dict) -> dict | None:
    path = _first_current_attachment_path(payload)
    if not path:
        return None
    user_text = _extract_latest_user_text(payload.get('messages'))
    if not _is_heruvim_document_query(user_text):
        return None
    if _HERUVIM_DOCUMENT_EDIT_QUERY_RE.search(user_text):
        return None
    if _HERUVIM_INDEXED_CORPUS_QUERY_RE.search(user_text):
        return None
    return {
        'id': 'heruvim_current_attachment_1',
        'type': 'function',
        'function': {
            'name': _HERUVIM_READ_DOCUMENT_TOOL_NAME,
            'arguments': json.dumps(
                {
                    'path': path,
                    'max_pages': 20,
                    'max_chars_per_page': 12000,
                },
                ensure_ascii=False,
            ),
        },
    }


def _parse_tool_arguments(tool_call: dict) -> dict:
    function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
    raw_arguments = function.get('arguments') or '{}'
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_tool_content(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _request_chat_completion_json(
    request: Request,
    *,
    request_url: str,
    payload: dict,
    headers: dict,
    cookies: dict,
    provider_url: str,
    api_key: str,
    requested_model: str | None,
    user,
) -> dict:
    body = copy.deepcopy(payload)
    body['stream'] = False
    body.pop('stream_options', None)

    r = None
    try:
        session = await get_session()
        r = await session.request(
            method='POST',
            url=request_url,
            data=json.dumps(body),
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=False),
        )
        try:
            response = await r.json(loads=JSONCodec.loads)
        except Exception:
            response = await r.text()

        if r.status >= 400:
            await publish_model_provider_request_failed(
                request,
                actor=user,
                provider='openai-compatible',
                base_url=provider_url,
                api_key=api_key,
                status=r.status,
                requested_model=requested_model,
                upstream_error=response,
            )
            raise HTTPException(status_code=r.status, detail=response)

        if not isinstance(response, dict):
            raise HTTPException(status_code=502, detail='Invalid non-streaming chat completion response')
        return response
    finally:
        await cleanup_response(r)


async def _apply_heruvim_ragflow_tool_loop(
    request: Request,
    *,
    payload: dict,
    request_url: str,
    headers: dict,
    cookies: dict,
    provider_url: str,
    api_key: str,
    requested_model: str | None,
    user,
) -> dict:
    public_base_url = await _heruvim_public_base_url(request)
    planning_payload = copy.deepcopy(payload)
    _add_heruvim_ragflow_tool(planning_payload)
    _add_heruvim_ragflow_tool_prompt(planning_payload)
    messages = planning_payload.get('messages')
    if not isinstance(messages, list):
        return payload

    contexts: list[str] = []
    executed_any = False

    async def execute_calls(tool_calls: list[dict], log_label: str) -> None:
        nonlocal executed_any
        normalized_calls = []
        results = []
        for index, tool_call in enumerate(tool_calls[:3], start=1):
            function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
            name = _normalize_heruvim_tool_name(str(function.get('name') or ''))
            arguments = _normalize_heruvim_tool_arguments(name, _parse_tool_arguments(tool_call))
            normalized_call = {
                'id': str(tool_call.get('id') or f'heruvim_tool_{len(contexts) + index}'),
                'type': 'function',
                'function': {
                    'name': name,
                    'arguments': json.dumps(arguments, ensure_ascii=False),
                },
            }
            try:
                result = await _execute_heruvim_tool(
                    normalized_call,
                    public_base_url=public_base_url,
                    user=user,
                    request=request,
                )
            except Exception as exc:
                log.exception('%s', log_label)
                result = {'ok': False, 'error': str(exc)}
            normalized_calls.append(normalized_call)
            results.append(result)
            contexts.append(_format_heruvim_tool_context(normalized_call, result))

        if not normalized_calls:
            return
        executed_any = True
        messages.append({'role': 'assistant', 'content': None, 'tool_calls': normalized_calls})
        for tool_call, result in zip(normalized_calls, results):
            messages.append(
                {
                    'role': 'tool',
                    'tool_call_id': tool_call['id'],
                    'name': tool_call['function']['name'],
                    'content': _json_tool_content(result),
                }
            )

    # Reading a file explicitly attached to this chat is mandatory and does
    # not need a model routing decision. Execute it before generation so even
    # providers that serialize tool intent as DSML cannot leak that syntax to
    # the user or skip document inspection.
    attached_tool_call = _synthesize_current_attachment_tool_call(planning_payload)
    if attached_tool_call:
        await execute_calls([attached_tool_call], 'HERUVIM attached document execution failed')

    # A document workflow may require several dependent actions, for example
    # RAGFlow search -> DOCX creation. Keep planning internally until the model
    # stops requesting tools so DSML or function-call markup never reaches the
    # streaming response shown to the user.
    for _ in range(4):
        response = await _request_chat_completion_json(
            request,
            request_url=request_url,
            payload=planning_payload,
            headers=headers,
            cookies=cookies,
            provider_url=provider_url,
            api_key=api_key,
            requested_model=requested_model,
            user=user,
        )

        assistant_message, tool_calls = _extract_chat_completion_tool_calls(response)
        if not tool_calls:
            tool_calls = _extract_dsml_heruvim_tool_calls(assistant_message)

        if tool_calls:
            await execute_calls(tool_calls, 'HERUVIM document tool execution failed')
            continue

        if not executed_any:
            return payload

        draft = ''
        if isinstance(assistant_message, dict):
            content = assistant_message.get('content')
            if isinstance(content, str):
                draft = content.strip()
            elif isinstance(content, list):
                draft = '\n'.join(
                    str(item.get('text') or item.get('content') or '')
                    for item in content
                    if isinstance(item, dict)
                ).strip()
        context = '\n\n'.join(contexts)
        if draft:
            context += (
                '\n\nHERUVIM_INTERNAL_DRAFT_RESPONSE:\n'
                + draft
                + '\nUse this draft only as supporting text. Do not expose internal protocol markup.'
            )
        return _prepare_heruvim_tool_result_payload(payload, context)

    return _prepare_heruvim_tool_result_payload(
        payload,
        '\n\n'.join(contexts)
        + '\n\nThe document tool limit was reached. Summarize completed results and do not call another tool.',
    )


def _is_heruvim_document_query(query: str) -> bool:
    if not query:
        return False
    return bool(_HERUVIM_DOCUMENT_QUERY_RE.search(query))


def _should_use_heruvim_ragflow(query: str) -> bool:
    if not query:
        return False
    if HERUVIM_RAGFLOW_AUTO_RETRIEVAL_MODE in {'always', 'all', 'force'}:
        return True
    return _is_heruvim_document_query(query)


def _ragflow_chunk_value(chunk: dict, *keys: str) -> Any:
    for key in keys:
        value = chunk.get(key)
        if value not in (None, ''):
            return value
    return None


def _extract_ragflow_chunks(response: Any) -> list[dict]:
    if not isinstance(response, dict):
        return []

    data = response.get('data', response)
    if isinstance(data, dict):
        for key in ('chunks', 'chunks_with_keywords', 'records', 'items', 'documents'):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if isinstance(data.get('doc_aggs'), list) and isinstance(data.get('chunks'), list):
            return [item for item in data['chunks'] if isinstance(item, dict)]
    elif isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    return []


def _extract_ragflow_doc_names(response: Any) -> dict[str, str]:
    if not isinstance(response, dict):
        return {}
    data = response.get('data', response)
    if not isinstance(data, dict):
        return {}
    doc_names = {}
    for item in data.get('doc_aggs') or []:
        if not isinstance(item, dict):
            continue
        doc_id = item.get('doc_id') or item.get('document_id') or item.get('id')
        doc_name = item.get('doc_name') or item.get('document_name') or item.get('name')
        if doc_id and doc_name:
            doc_names[str(doc_id)] = str(doc_name)
    return doc_names


async def _heruvim_ragflow_dataset_ids() -> list[str]:
    if HERUVIM_RAGFLOW_DATASET_IDS:
        return list(dict.fromkeys(HERUVIM_RAGFLOW_DATASET_IDS))
    if not HERUVIM_RAGFLOW_CHAT_ID:
        return []

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f'{HERUVIM_RAGFLOW_BASE_URL}/api/v1/chats/{HERUVIM_RAGFLOW_CHAT_ID}',
            headers={'Authorization': f'Bearer {HERUVIM_RAGFLOW_API_KEY}'},
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                log.warning('Failed to resolve RAGFlow chat datasets: HTTP %s %s', response.status, text[:500])
                return []
            try:
                data = json.loads(text)
            except Exception:
                log.warning('Failed to parse RAGFlow chat datasets response')
                return []

    chat = data.get('data') if isinstance(data, dict) else {}
    if not isinstance(chat, dict):
        return []
    dataset_ids = chat.get('dataset_ids') or chat.get('kb_ids') or []
    if not isinstance(dataset_ids, list):
        return []
    return [str(value) for value in dataset_ids if value]


async def _heruvim_ragflow_download_document(document_id: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            f'{HERUVIM_RAGFLOW_BASE_URL}/api/v1/documents/{document_id}/preview',
            headers={'Authorization': f'Bearer {HERUVIM_RAGFLOW_API_KEY}'},
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            content = await response.read()
            content_type = response.headers.get('Content-Type') or ''
            disposition = response.headers.get('Content-Disposition') or ''
            if response.status >= 400:
                return {
                    'ok': False,
                    'document_id': document_id,
                    'status_code': response.status,
                    'error': content[:1000].decode(errors='ignore'),
                }
            return {
                'ok': True,
                'document_id': document_id,
                'content': content,
                'content_type': content_type,
                'content_disposition': disposition,
            }


def _filename_from_disposition(disposition: str) -> str:
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition or '', re.I)
    if not match:
        return ''
    return match.group(1).strip()


def _extract_pdf_text_from_bytes(content: bytes, max_chars: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    parts = []
    total = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ''
        except Exception as exc:
            text = f'[page {index}: text extraction failed: {exc}]'
        if text.strip():
            page_text = f'\n\n--- page {index} ---\n{text.strip()}'
            parts.append(page_text)
            total += len(page_text)
        if total >= max_chars:
            break
    result = ''.join(parts).strip()
    if len(result) > max_chars:
        result = result[:max_chars].rstrip() + '\n\n[full document text trimmed]'
    return result


def _extract_docx_text_from_bytes(content: bytes, max_chars: int) -> str:
    import docx2txt

    with tempfile.NamedTemporaryFile(suffix='.docx') as tmp:
        tmp.write(content)
        tmp.flush()
        text = docx2txt.process(tmp.name) or ''
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + '\n\n[full document text trimmed]'
    return text


def _extract_text_from_bytes(content: bytes, max_chars: int) -> tuple[str, str]:
    encoding = 'utf-8'
    for candidate in ('utf-8', 'utf-8-sig', 'cp1251', 'latin-1'):
        try:
            text = content.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    else:
        text = content.decode('utf-8', errors='replace')
        encoding = 'utf-8-replace'
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + '\n\n[document text trimmed]'
    return text, encoding


async def _execute_heruvim_read_document_tool(tool_call: dict) -> dict:
    arguments = _parse_tool_arguments(tool_call)
    raw_path = str(arguments.get('path') or '').strip()
    if not raw_path:
        return {'ok': False, 'tool': _HERUVIM_READ_DOCUMENT_TOOL_NAME, 'error': 'Missing required argument: path'}

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        return {
            'ok': False,
            'tool': _HERUVIM_READ_DOCUMENT_TOOL_NAME,
            'path': str(path),
            'error': 'Document path does not exist or is not a file',
        }

    try:
        max_pages = max(1, min(500, int(arguments.get('max_pages', 20))))
    except Exception:
        max_pages = 20
    try:
        max_chars_per_page = max(1000, min(50000, int(arguments.get('max_chars_per_page', 12000))))
    except Exception:
        max_chars_per_page = 12000
    max_chars = max_pages * max_chars_per_page

    try:
        content = await asyncio.to_thread(path.read_bytes)
        suffix = path.suffix.lower()
        if suffix == '.pdf' or content.lstrip().startswith(b'%PDF'):
            text = _extract_pdf_text_from_bytes(content, max_chars)
            doc_type = 'pdf'
            extra = {}
        elif suffix == '.docx':
            text = _extract_docx_text_from_bytes(content, max_chars)
            doc_type = 'docx'
            extra = {}
        else:
            text, encoding = _extract_text_from_bytes(content, max_chars)
            doc_type = suffix.lstrip('.') or 'text'
            extra = {'encoding': encoding}
        return {
            'ok': True,
            'tool': _HERUVIM_READ_DOCUMENT_TOOL_NAME,
            'path': str(path),
            'name': path.name,
            'type': doc_type,
            'byte_count': len(content),
            'char_count': len(text),
            **extra,
            'text': text,
        }
    except Exception as exc:
        log.exception('HERUVIM local document read failed')
        return {
            'ok': False,
            'tool': _HERUVIM_READ_DOCUMENT_TOOL_NAME,
            'path': str(path),
            'error': str(exc),
        }


def _extract_document_text_from_download(download: dict, fallback_name: str) -> dict:
    if not download.get('ok'):
        return {**download, 'text': ''}
    content = download.get('content') or b''
    content_type = (download.get('content_type') or '').lower()
    filename = _filename_from_disposition(download.get('content_disposition') or '') or fallback_name
    lower_name = filename.lower()
    try:
        if 'pdf' in content_type or lower_name.endswith('.pdf') or content.lstrip().startswith(b'%PDF'):
            text = _extract_pdf_text_from_bytes(content, HERUVIM_RAGFLOW_FULL_DOCUMENT_MAX_CHARS)
        elif 'wordprocessingml' in content_type or lower_name.endswith('.docx'):
            text = _extract_docx_text_from_bytes(content, HERUVIM_RAGFLOW_FULL_DOCUMENT_MAX_CHARS)
        else:
            text = content.decode('utf-8', errors='ignore')
            if len(text) > HERUVIM_RAGFLOW_FULL_DOCUMENT_MAX_CHARS:
                text = text[:HERUVIM_RAGFLOW_FULL_DOCUMENT_MAX_CHARS].rstrip() + '\n\n[full document text trimmed]'
        return {
            'ok': True,
            'document_id': download.get('document_id'),
            'name': filename,
            'content_type': content_type,
            'byte_count': len(content),
            'preview_url': f'/api/v1/heruvim/ragflow/documents/{download.get("document_id")}/preview',
            'download_url': f'/api/v1/heruvim/ragflow/documents/{download.get("document_id")}/preview?download=1',
            'text': text,
        }
    except Exception as exc:
        return {
            'ok': False,
            'document_id': download.get('document_id'),
            'name': filename,
            'content_type': content_type,
            'byte_count': len(content),
            'error': str(exc),
            'text': '',
        }


async def _heruvim_ragflow_full_documents(chunks: list[dict], doc_names: dict[str, str]) -> list[dict]:
    if not HERUVIM_RAGFLOW_FULL_DOCUMENTS or HERUVIM_RAGFLOW_FULL_DOCUMENT_LIMIT <= 0:
        return []

    ordered_doc_ids = []
    for chunk in chunks:
        doc_id = _ragflow_chunk_value(chunk, 'document_id', 'doc_id')
        if doc_id and str(doc_id) not in ordered_doc_ids:
            ordered_doc_ids.append(str(doc_id))
        if len(ordered_doc_ids) >= HERUVIM_RAGFLOW_FULL_DOCUMENT_LIMIT:
            break

    documents = []
    for doc_id in ordered_doc_ids:
        download = await _heruvim_ragflow_download_document(doc_id)
        documents.append(_extract_document_text_from_download(download, doc_names.get(doc_id, doc_id)))
    return documents


async def _heruvim_ragflow_retrieve(query: str, page_size: int | None = None) -> dict:
    if not HERUVIM_RAGFLOW_ENABLED:
        return {'ok': False, 'error': 'RAGFlow is not configured'}

    dataset_ids = await _heruvim_ragflow_dataset_ids()
    if not dataset_ids:
        return {'ok': False, 'error': 'RAGFlow chat assistant has no connected datasets'}

    result_limit = HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE
    if page_size is not None:
        result_limit = max(1, min(30, int(page_size)))

    payload = {
        'question': query,
        'dataset_ids': dataset_ids,
        'document_ids': [],
        'page': 1,
        'page_size': result_limit,
        'similarity_threshold': HERUVIM_RAGFLOW_RETRIEVAL_SIMILARITY_THRESHOLD,
        'vector_similarity_weight': HERUVIM_RAGFLOW_RETRIEVAL_VECTOR_WEIGHT,
        'keyword': True,
        'top_k': 1024,
    }

    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f'{HERUVIM_RAGFLOW_BASE_URL}/api/v1/retrieval',
            json=payload,
            headers={'Authorization': f'Bearer {HERUVIM_RAGFLOW_API_KEY}'},
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            text = await response.text()
            try:
                data = json.loads(text)
            except Exception:
                data = {'raw': text[:1000]}
            if response.status >= 400:
                return {'ok': False, 'status_code': response.status, 'response': data}
            chunks = _extract_ragflow_chunks(data)
            doc_names = _extract_ragflow_doc_names(data)
            full_documents = await _heruvim_ragflow_full_documents(chunks, doc_names)
            return {
                'ok': True,
                'response': data,
                'dataset_ids': dataset_ids,
                'doc_names': doc_names,
                'chunks': chunks[:result_limit],
                'full_documents': full_documents,
            }


def _apply_heruvim_document_urls(retrieval: dict, public_base_url: str = '') -> dict:
    if not public_base_url:
        return retrieval

    for document in retrieval.get('full_documents') or []:
        document_id = document.get('document_id')
        if document_id:
            document['preview_url'] = _heruvim_document_url(str(document_id), public_base_url)
            document['download_url'] = _heruvim_document_url(str(document_id), public_base_url, download=True)

    for chunk in retrieval.get('chunks') or []:
        doc_id = _ragflow_chunk_value(chunk, 'document_id', 'doc_id', 'id')
        if doc_id:
            chunk['preview_url'] = _heruvim_document_url(str(doc_id), public_base_url)
            chunk['download_url'] = _heruvim_document_url(str(doc_id), public_base_url, download=True)
    return retrieval


async def _execute_heruvim_ragflow_tool(tool_call: dict, *, public_base_url: str = '') -> dict:
    function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
    if function.get('name') != _HERUVIM_RAGFLOW_TOOL_NAME:
        return {'ok': False, 'error': f"Unknown tool: {function.get('name')}"}

    arguments = _parse_tool_arguments(tool_call)
    query = str(arguments.get('query') or '').strip()
    if not query:
        return {'ok': False, 'error': 'Missing required argument: query'}

    limit = arguments.get('limit', HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE)
    try:
        limit = max(1, min(30, int(limit)))
    except Exception:
        limit = HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE

    retrieval = await _heruvim_ragflow_retrieve(query, page_size=limit)
    retrieval = _apply_heruvim_document_urls(retrieval, public_base_url)
    retrieval['tool'] = _HERUVIM_RAGFLOW_TOOL_NAME
    retrieval['query'] = query
    return retrieval


async def _execute_heruvim_local_openapi_tool(tool_call: dict) -> dict:
    function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
    name = _normalize_heruvim_tool_name(str(function.get('name') or ''))
    target = _HERUVIM_LOCAL_OPENAPI_TOOLS.get(name)
    if not target:
        return {'ok': False, 'tool': name, 'error': f'Unknown local OpenAPI tool: {name}'}

    method, base_url, path = target
    arguments = _normalize_heruvim_tool_arguments(name, _parse_tool_arguments(tool_call))
    timeout = aiohttp.ClientTimeout(total=900)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            request_kwargs = {'ssl': AIOHTTP_CLIENT_SESSION_SSL}
            if method == 'POST':
                request_kwargs['json'] = arguments
            async with session.request(method, f'{base_url}{path}', **request_kwargs) as response:
                raw = await response.text()
                try:
                    result = json.loads(raw)
                except Exception:
                    result = {'response': raw}
                if not isinstance(result, dict):
                    result = {'response': result}
                result.setdefault('ok', response.status < 400)
                result['tool'] = name
                result['status_code'] = response.status
                if response.status >= 400:
                    result.setdefault('error', raw[:2000])
                return result
    except Exception as exc:
        log.exception('HERUVIM local OpenAPI tool request failed: %s', name)
        return {'ok': False, 'tool': name, 'error': str(exc)}


async def _register_heruvim_artifact(
    result: dict,
    tool_name: str,
    user: UserModel | None,
    public_base_url: str = '',
    request: Request | None = None,
) -> dict:
    if not result.get('ok') or tool_name not in _HERUVIM_ARTIFACT_TOOLS or not user:
        return result

    raw_path = result.get('output_path') or (result.get('path') if tool_name == 'heruvim_docx_create' else None)
    if not isinstance(raw_path, str) or not raw_path:
        return result

    source_path = Path(raw_path).expanduser().resolve()
    if not source_path.is_file():
        result['artifact_error'] = f'Generated file was not found: {source_path}'
        return result

    file_id = str(uuid4())
    display_name = source_path.name
    storage_name = f'{file_id}_{display_name}'
    tags = {
        'OpenWebUI-User-Id': user.id,
        'OpenWebUI-File-Id': file_id,
        'OpenWebUI-Generated-By': tool_name,
    }

    try:
        with source_path.open('rb') as file_handle:
            contents, storage_path = await asyncio.to_thread(
                Storage.upload_file,
                file_handle,
                storage_name,
                tags,
            )
        content_type = mimetypes.guess_type(display_name)[0] or 'application/octet-stream'
        file_item = await Files.insert_new_file(
            user.id,
            FileForm(
                id=file_id,
                hash=hashlib.sha256(contents).hexdigest(),
                filename=display_name,
                path=storage_path,
                data={'status': 'completed', 'heruvim_generated': True},
                meta={
                    'name': display_name,
                    'content_type': content_type,
                    'size': len(contents),
                    'generated_by': tool_name,
                },
            ),
        )
        if not file_item:
            raise RuntimeError('Open WebUI did not create a file record')
    except Exception as exc:
        log.exception('HERUVIM generated artifact registration failed: %s', source_path)
        result['artifact_error'] = str(exc)
        return result

    base = public_base_url.rstrip('/')
    preview_url = f'{base}/api/v1/files/{file_id}/content'
    download_url = f'{preview_url}?attachment=true'
    result['artifact'] = {
        'id': file_id,
        'name': display_name,
        'content_type': content_type,
        'size': len(contents),
        'preview_url': preview_url,
        'download_url': download_url,
    }
    result['preview_url'] = preview_url
    result['download_url'] = download_url
    if request is not None:
        artifacts = list(getattr(request.state, 'heruvim_artifacts', []) or [])
        if not any(item.get('id') == file_id for item in artifacts if isinstance(item, dict)):
            artifacts.append(result['artifact'])
        request.state.heruvim_artifacts = artifacts
    return result


async def _execute_heruvim_tool(
    tool_call: dict,
    *,
    public_base_url: str = '',
    user: UserModel | None = None,
    request: Request | None = None,
) -> dict:
    function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
    name = _normalize_heruvim_tool_name(str(function.get('name') or ''))
    if name == _HERUVIM_RAGFLOW_TOOL_NAME:
        return await _execute_heruvim_ragflow_tool(tool_call, public_base_url=public_base_url)
    if name == _HERUVIM_READ_DOCUMENT_TOOL_NAME:
        return await _execute_heruvim_read_document_tool(tool_call)
    if name in _HERUVIM_LOCAL_OPENAPI_TOOLS:
        result = await _execute_heruvim_local_openapi_tool(tool_call)
        return await _register_heruvim_artifact(result, str(name), user, public_base_url, request)
    return {'ok': False, 'error': f'Unknown Heruvim tool: {name}'}


def _format_heruvim_tool_context(tool_call: dict, result: dict) -> str:
    function = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
    name = function.get('name')
    if name == _HERUVIM_RAGFLOW_TOOL_NAME:
        query = _parse_tool_arguments(tool_call).get('query') or ''
        return _format_ragflow_context(str(query), result)

    if name == _HERUVIM_READ_DOCUMENT_TOOL_NAME:
        if not result.get('ok'):
            return (
                'HERUVIM_LOCAL_DOCUMENT_READ_STATUS: failed\n'
                f'path="{result.get("path")}"\n'
                f'error="{result.get("error")}"\n'
                'Answer that the attached document could not be read and give the error.'
            )
        return (
            'HERUVIM_LOCAL_DOCUMENT_READ_STATUS: ok\n'
            f'document="{result.get("name")}" path="{result.get("path")}" type="{result.get("type")}" '
            f'bytes="{result.get("byte_count")}" chars="{result.get("char_count")}"\n'
            f'{result.get("text") or ""}'
        )

    if name in _HERUVIM_LOCAL_OPENAPI_TOOLS:
        status = 'ok' if result.get('ok') else 'failed'
        return (
            f'HERUVIM_DOCUMENT_TOOL_STATUS: {status}\n'
            f'tool="{name}"\n'
            f'{_json_tool_content(result)}'
        )

    return f'HERUVIM_TOOL_STATUS: failed\nerror="{result.get("error")}"'


def _format_ragflow_context(query: str, retrieval: dict, *, public_base_url: str = '') -> str:
    if not retrieval.get('ok'):
        return (
            'RAGFLOW_DOCUMENT_SEARCH_STATUS: failed\n'
            f'Question: {query}\n'
            f'Error: {retrieval.get("error") or retrieval.get("response")}\n'
            'Answer honestly that the document search subsystem is unavailable; do not use Open WebUI knowledge bases as a substitute.'
        )

    chunks = retrieval.get('chunks') or []
    doc_names = retrieval.get('doc_names') or {}
    if not chunks:
        return (
            'RAGFLOW_DOCUMENT_SEARCH_STATUS: no_hits\n'
            f'Question: {query}\n'
            'RAGFlow returned no chunks. Answer that no indexed document evidence was found for this request.'
        )

    lines = [
        'RAGFLOW_DOCUMENT_SEARCH_STATUS: hits',
        f'Question: {query}',
        'Use ONLY the RAGFlow evidence below for document facts. Do not claim that Open WebUI knowledge bases are empty.',
        'If the user asks to find a document, return matching document names/ids and the exact source snippets.',
        '',
        'RAGFlow evidence:',
    ]
    full_documents = retrieval.get('full_documents') or []
    if full_documents:
        lines.extend(['', 'Full document text fetched through RAGFlow preview/MinIO bridge:'])
        for doc in full_documents:
            if not doc.get('ok'):
                lines.append(
                    f'document="{doc.get("name") or doc.get("document_id")}" document_id="{doc.get("document_id")}" '
                    f'full_text_status="failed" error="{doc.get("error")}"'
                )
                continue
            text = (doc.get('text') or '').strip()
            doc_preview_url = doc.get('preview_url') or _heruvim_document_url(str(doc.get('document_id') or ''))
            doc_download_url = doc.get('download_url') or _heruvim_document_url(
                str(doc.get('document_id') or ''),
                download=True,
            )
            if public_base_url:
                doc_preview_url = _heruvim_document_url(str(doc.get('document_id') or ''), public_base_url)
                doc_download_url = _heruvim_document_url(
                    str(doc.get('document_id') or ''),
                    public_base_url,
                    download=True,
                )
            if not text:
                lines.append(
                    f'document="{doc.get("name")}" document_id="{doc.get("document_id")}" '
                    f'preview_url="{doc_preview_url}" download_url="{doc_download_url}" '
                    'full_text_status="empty"'
                )
                continue
            lines.append(
                f'document="{doc.get("name")}" document_id="{doc.get("document_id")}" '
                f'bytes="{doc.get("byte_count")}" preview_url="{doc_preview_url}" '
                f'download_url="{doc_download_url}" full_text_status="ok"\n{text}'
            )
        lines.append('')

    lines.append('Top retrieval chunks:')
    for index, chunk in enumerate(chunks[:HERUVIM_RAGFLOW_RETRIEVAL_PAGE_SIZE], start=1):
        doc_id = _ragflow_chunk_value(chunk, 'document_id', 'doc_id', 'id') or ''
        doc_name = (
            _ragflow_chunk_value(chunk, 'document_name', 'doc_name', 'document_keyword', 'name', 'filename')
            or doc_names.get(str(doc_id))
            or 'unknown document'
        )
        page = _ragflow_chunk_value(chunk, 'page', 'page_num', 'page_number', 'position') or ''
        score = _ragflow_chunk_value(chunk, 'similarity', 'score', 'vector_similarity') or ''
        text = _ragflow_chunk_value(chunk, 'content', 'text', 'chunk', 'body') or ''
        if isinstance(text, list):
            text = '\n'.join(str(item) for item in text)
        text = str(text).strip()
        if len(text) > 1800:
            text = text[:1800].rstrip() + '…'
        lines.append(
            f'[{index}] document="{doc_name}" document_id="{doc_id}" page="{page}" score="{score}" '
            f'preview_url="{_heruvim_document_url(str(doc_id), public_base_url)}" '
            f'download_url="{_heruvim_document_url(str(doc_id), public_base_url, download=True)}"\n{text}'
        )
    return '\n\n'.join(lines)


def _inject_system_context(payload: dict, context: str) -> None:
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return
    system_message = next((message for message in messages if message.get('role') in {'system', 'developer'}), None)
    if system_message and isinstance(system_message.get('content'), str):
        system_message['content'] = f"{system_message['content']}\n\n{context}"
    else:
        messages.insert(0, {'role': 'system', 'content': context})
_MODEL_LIST_TIMEOUT = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
_UNSUPPORTED_OPENAI_MODEL_KEYWORDS = ('babbage', 'dall-e', 'davinci', 'embedding', 'tts', 'whisper')


def _clean_proxy_headers(raw_headers) -> dict:
    """Return a copy of *raw_headers* with stale encoding headers removed."""
    return {k: v for k, v in raw_headers.items() if k not in _STRIP_PROXY_HEADERS}


async def send_get_request(
    request: Request = None,
    url=None,
    key=None,
    user: UserModel = None,
    config=None,
):
    try:
        async with aiohttp.ClientSession(timeout=_MODEL_LIST_TIMEOUT, trust_env=True) as session:
            if request and config:
                headers, cookies = await get_headers_and_cookies(request, url, key, config, user=user)
            else:
                headers = {
                    **({'Authorization': f'Bearer {key}'} if key else {}),
                }
                cookies = None

                if ENABLE_FORWARD_USER_INFO_HEADERS and user:
                    headers = include_user_info_headers(headers, user)

            async with session.get(
                url,
                headers=headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as response:
                return await response.json(loads=JSONCodec.loads)
    except Exception as e:
        # Handle connection error here
        log.error(f'Connection error: {e}')
        return None


async def get_models_request(
    request: Request = None,
    url=None,
    key=None,
    user: UserModel = None,
    config=None,
):
    if is_anthropic_url(url):
        return await get_anthropic_models(url, key, user=user)
    return await send_get_request(request, f'{url}/models', key, user=user, config=config)


def openai_reasoning_model_handler(payload):
    """
    Handle reasoning model specific parameters
    """
    if 'max_tokens' in payload:
        # Convert "max_tokens" to "max_completion_tokens" for all reasoning models
        payload['max_completion_tokens'] = payload['max_tokens']
        del payload['max_tokens']

    # Handle system role conversion based on model type
    if payload['messages'][0]['role'] == 'system':
        model_lower = payload['model'].lower()
        # Legacy models use "user" role instead of "system"
        if model_lower.startswith('o1-mini') or model_lower.startswith('o1-preview'):
            payload['messages'][0]['role'] = 'user'
        else:
            payload['messages'][0]['role'] = 'developer'

    return payload


async def get_headers_and_cookies(
    request: Request,
    url,
    key=None,
    config=None,
    metadata: dict | None = None,
    user: UserModel = None,
):
    cookies = {}
    headers = {
        'Content-Type': 'application/json',
        **(
            {
                'HTTP-Referer': 'https://openwebui.com/',
                'X-Title': 'Open WebUI',
            }
            if 'openrouter.ai' in url
            else {}
        ),
    }

    if ENABLE_FORWARD_USER_INFO_HEADERS and user:
        headers = include_user_info_headers(headers, user)
        if metadata and metadata.get('chat_id'):
            headers[FORWARD_SESSION_INFO_HEADER_CHAT_ID] = metadata.get('chat_id')

    token = None
    auth_type = config.get('auth_type')

    if auth_type == 'bearer' or auth_type is None:
        # Default to bearer if not specified
        token = f'{key}'
    elif auth_type == 'none':
        token = None
    elif auth_type == 'session':
        cookies = request.cookies
        token = request.state.token.credentials
    elif auth_type == 'system_oauth':
        cookies = request.cookies

        oauth_token = None
        try:
            if request.cookies.get('oauth_session_id', None):
                oauth_token = await request.app.state.oauth_manager.get_oauth_token(
                    user.id,
                    request.cookies.get('oauth_session_id', None),
                )
        except Exception as e:
            log.error(f'Error getting OAuth token: {e}')

        if oauth_token:
            token = f'{oauth_token.get("access_token", "")}'

    elif auth_type in ('azure_ad', 'microsoft_entra_id'):
        token = get_microsoft_entra_id_access_token()

    if token:
        headers['Authorization'] = f'Bearer {token}'

    if config.get('headers') and isinstance(config.get('headers'), dict):
        custom_headers = await get_custom_headers(config.get('headers'), user, metadata, request=request)
        headers.update(custom_headers)

    return headers, cookies


def get_microsoft_entra_id_access_token():
    """
    Get Microsoft Entra ID access token using DefaultAzureCredential for Azure OpenAI.
    Returns the token string or None if authentication fails.
    """
    try:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), 'https://cognitiveservices.azure.com/.default'
        )
        return token_provider()
    except Exception as e:
        log.error(f'Error getting Microsoft Entra ID access token: {e}')
        return None


##########################################
#
# API routes
#
##########################################

router = APIRouter()

LLAMACPP_LOADED_STATES = {'loaded', 'sleeping'}
LLAMACPP_UNLOADED_STATES = {'loading', 'unloaded'}


def get_llamacpp_model_loaded_state(model: dict, provider: str, manual_model_ids: bool = False) -> bool | None:
    if provider != 'llama.cpp':
        return None

    status = model.get('status')
    if isinstance(status, dict):
        value = status.get('value')
        if value in LLAMACPP_LOADED_STATES:
            return True
        if value in LLAMACPP_UNLOADED_STATES:
            return False

    if not manual_model_ids and 'status' not in model:
        return True

    return None


OPENAI_CONFIG_KEYS = {
    'ENABLE_OPENAI_API': 'openai.enable',
    'OPENAI_API_BASE_URLS': 'openai.api_base_urls',
    'OPENAI_API_KEYS': 'openai.api_keys',
    'OPENAI_API_CONFIGS': 'openai.api_configs',
}


async def get_openai_config() -> dict:
    values = await Config.get_many(*OPENAI_CONFIG_KEYS.values())
    return {field: values[storage_key] for field, storage_key in OPENAI_CONFIG_KEYS.items() if storage_key in values}


async def get_openai_runtime_config() -> tuple[bool, list[str], list[str], dict]:
    # The ХЕРУВИМ connection is environment-owned and must win over stale
    # persistent Open WebUI connection settings. Keeping this overlay runtime-
    # only avoids copying API keys into the config database and guarantees that
    # the product exposes exactly one primary model route.
    if HERUVIM_LLM_ENABLED:
        return (
            True,
            [HERUVIM_LLM_BASE_URL],
            [HERUVIM_LLM_API_KEY],
            {
                '0': {
                    'enable': True,
                    'provider': 'openai',
                    'model_ids': [HERUVIM_LLM_MODEL],
                    'model_names': {HERUVIM_LLM_MODEL: HERUVIM_LLM_DISPLAY_NAME},
                    'tags': [{'name': 'LLM'}],
                }
            },
        )

    if HERUVIM_RAGFLOW_ENABLED and HERUVIM_RAGFLOW_DIRECT_CHAT_FALLBACK:
        ragflow_url = f'{HERUVIM_RAGFLOW_BASE_URL}/api/v1/openai/{HERUVIM_RAGFLOW_CHAT_ID}'
        return (
            True,
            [ragflow_url],
            [HERUVIM_RAGFLOW_API_KEY],
            {
                '0': {
                    'enable': True,
                    'provider': 'ragflow',
                    'model_ids': ['model'],
                    'model_names': {'model': 'ХЕРУВИМ'},
                    'tags': [{'name': 'Локальная база знаний'}],
                    'ragflow_references': True,
                }
            },
        )

    values = await Config.get_many('openai.enable', 'openai.api_base_urls', 'openai.api_keys', 'openai.api_configs')
    return (
        values.get('openai.enable'),
        values.get('openai.api_base_urls') or [],
        values.get('openai.api_keys') or [],
        values.get('openai.api_configs') or {},
    )


async def normalize_openai_api_keys(api_base_urls: list[str], api_keys: list[str]) -> list[str]:
    if len(api_keys) > len(api_base_urls):
        api_keys = api_keys[: len(api_base_urls)]
    elif len(api_keys) < len(api_base_urls):
        api_keys = [*api_keys, *([''] * (len(api_base_urls) - len(api_keys)))]

    await Config.upsert({'openai.api_keys': api_keys})
    return api_keys


async def get_openai_connection(idx: int) -> tuple[str, str, dict]:
    _, api_base_urls, api_keys, api_configs = await get_openai_runtime_config()
    url = api_base_urls[idx]
    key = api_keys[idx]
    api_config = api_configs.get(str(idx), api_configs.get(url, {}))
    return url, key, api_config


async def get_anthropic_token_count_target(request: Request, form_data: dict, user: UserModel):
    """Resolve the upstream LiteLLM connection for an Anthropic token-count request."""
    requested_model = form_data.get('model')
    if not requested_model:
        raise HTTPException(status_code=400, detail='model is required')

    payload = {**form_data}
    model_id = requested_model
    model_info = await Models.get_model_by_id(model_id)
    await check_model_access(user, model_info, BYPASS_MODEL_ACCESS_CONTROL)

    if model_info and model_info.base_model_id:
        model_id = model_info.base_model_id
        payload['model'] = model_id

    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS

    model = models.get(model_id)
    if not model or 'urlIdx' not in model:
        raise HTTPException(status_code=404, detail=ERROR_MESSAGES.MODEL_NOT_FOUND())

    url, key, api_config = await get_openai_connection(model['urlIdx'])
    prefix_id = api_config.get('prefix_id')
    payload['model'] = strip_provider_model_prefix(payload['model'], prefix_id)

    headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)
    return requested_model, payload, url, key, headers, cookies


async def count_anthropic_tokens(request: Request, form_data: dict, user: UserModel) -> int:
    """Forward an Anthropic token-count request through an OpenAI-compatible connection."""
    requested_model, payload, url, key, headers, cookies = await get_anthropic_token_count_target(
        request, form_data, user
    )
    request_url = f'{url.rstrip("/")}/messages/count_tokens'
    response = None

    try:
        session = await get_session()
        response = await session.request(
            method='POST',
            url=request_url,
            data=json.dumps(payload),
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(),
        )

        try:
            response_data = await response.json(loads=JSONCodec.loads)
        except Exception:
            response_data = await response.text()

        if response.status >= 400:
            await publish_model_provider_request_failed(
                request,
                actor=user,
                provider='openai-compatible',
                base_url=url,
                api_key=key,
                status=response.status,
                requested_model=requested_model,
                upstream_error=response_data,
            )
            raise HTTPException(status_code=response.status, detail=response_data)

        input_tokens = response_data.get('input_tokens') if isinstance(response_data, dict) else None
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise HTTPException(status_code=502, detail='Invalid token-count response from upstream provider')

        return input_tokens
    except HTTPException:
        raise
    except Exception:
        log.exception('Failed to count Anthropic tokens for model %s', requested_model)
        raise HTTPException(status_code=502, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)
    finally:
        await cleanup_response(response)


@router.get('/config')
async def get_config(request: Request, user=Depends(get_admin_user)):
    return await get_openai_config()


class OpenAIConfigForm(BaseModel):
    ENABLE_OPENAI_API: bool | None = None
    OPENAI_API_BASE_URLS: list[str]
    OPENAI_API_KEYS: list[str]
    OPENAI_API_CONFIGS: dict


@router.post('/config/update')
async def update_config(request: Request, form_data: OpenAIConfigForm, user=Depends(get_admin_user)):
    api_keys = form_data.OPENAI_API_KEYS

    if len(api_keys) > len(form_data.OPENAI_API_BASE_URLS):
        api_keys = api_keys[: len(form_data.OPENAI_API_BASE_URLS)]
    elif len(api_keys) < len(form_data.OPENAI_API_BASE_URLS):
        api_keys = [*api_keys, *([''] * (len(form_data.OPENAI_API_BASE_URLS) - len(api_keys)))]

    valid_keys = set(map(str, range(len(form_data.OPENAI_API_BASE_URLS))))
    api_configs = {key: value for key, value in form_data.OPENAI_API_CONFIGS.items() if key in valid_keys}

    await Config.upsert(
        {
            'openai.enable': form_data.ENABLE_OPENAI_API,
            'openai.api_base_urls': form_data.OPENAI_API_BASE_URLS,
            'openai.api_keys': api_keys,
            'openai.api_configs': api_configs,
        }
    )

    await get_all_models.cache.clear()
    request.app.state.BASE_MODELS = []
    request.app.state.OPENAI_MODELS = {}
    models = getattr(request.app.state, 'MODELS', None)
    if hasattr(models, 'clear'):
        models.clear()
    else:
        request.app.state.MODELS = {}

    await publish_event(
        request,
        EVENTS.MODEL_PROVIDER_CONFIG_UPDATED,
        actor=user,
        subject_id='openai',
        subject_type='model.provider_config',
        data={
            'provider': 'openai',
            'enabled': form_data.ENABLE_OPENAI_API,
            'base_url_count': len(form_data.OPENAI_API_BASE_URLS),
        },
    )

    return {
        'ENABLE_OPENAI_API': form_data.ENABLE_OPENAI_API,
        'OPENAI_API_BASE_URLS': form_data.OPENAI_API_BASE_URLS,
        'OPENAI_API_KEYS': api_keys,
        'OPENAI_API_CONFIGS': api_configs,
    }


@router.post('/audio/speech')
async def speech(request: Request, user=Depends(get_verified_user)):
    if user.role != 'admin' and not await has_permission(user.id, 'chat.tts', await Config.get('user.permissions')):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    idx = None
    try:
        _, api_base_urls, _, _ = await get_openai_runtime_config()
        idx = api_base_urls.index('https://api.openai.com/v1')

        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = CACHE_DIR / 'audio' / 'speech'
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f'{name}.mp3')
        file_body_path = SPEECH_CACHE_DIR.joinpath(f'{name}.json')

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        url, key, api_config = await get_openai_connection(idx)

        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        r = None
        try:
            session = await get_session()
            r = await session.post(
                url=f'{url}/audio/speech',
                data=body,
                headers=headers,
                cookies=cookies,
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            )

            r.raise_for_status()

            async with aiofiles.open(file_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(8192):
                    await f.write(chunk)

            async with aiofiles.open(file_body_path, 'w') as f:
                await f.write(json.dumps(json.loads(body.decode('utf-8'))))

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = await r.json(loads=JSONCodec.loads)
                    if 'error' in res:
                        detail = f'External: {res["error"]}'
                except Exception:
                    detail = f'External: {e}'

            raise HTTPException(
                status_code=r.status if r else 500,
                detail=detail if detail else 'Open WebUI: Server Connection Error',
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.OPENAI_NOT_FOUND)


async def get_all_models_responses(request: Request, user: UserModel) -> list:
    enable_openai_api, api_base_urls, api_keys, api_configs = await get_openai_runtime_config()
    if not enable_openai_api:
        return []

    num_urls = len(api_base_urls)
    num_keys = len(api_keys)

    if num_keys != num_urls:
        api_keys = await normalize_openai_api_keys(api_base_urls, api_keys)

    request_tasks = []
    for idx, url in enumerate(api_base_urls):
        if (str(idx) not in api_configs) and (url not in api_configs):  # Legacy support
            request_tasks.append(get_models_request(request, url, api_keys[idx], user=user))
        else:
            api_config = api_configs.get(
                str(idx),
                api_configs.get(url, {}),  # Legacy support
            )

            enable = api_config.get('enable', True)
            model_ids = api_config.get('model_ids', [])
            model_names = api_config.get('model_names', {})

            if enable:
                if len(model_ids) == 0:
                    request_tasks.append(get_models_request(request, url, api_keys[idx], user=user, config=api_config))
                else:
                    model_list = {
                        'object': 'list',
                        'data': [
                            {
                                'id': model_id,
                                'name': model_names.get(model_id, model_id),
                                'owned_by': 'openai',
                                'openai': {'id': model_id},
                                'urlIdx': idx,
                            }
                            for model_id in model_ids
                        ],
                    }

                    request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, model_list)))
            else:
                request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, None)))

    responses = await asyncio.gather(*request_tasks)

    for idx, response in enumerate(responses):
        if response:
            url = api_base_urls[idx]
            api_config = api_configs.get(
                str(idx),
                api_configs.get(url, {}),  # Legacy support
            )

            connection_type = api_config.get('connection_type', 'external')
            prefix_id = api_config.get('prefix_id', None)
            tags = api_config.get('tags', [])
            provider = api_config.get('provider', '')

            model_list = response if isinstance(response, list) else response.get('data', [])
            if not isinstance(model_list, list):
                # Catch non-list responses
                model_list = []

            for model in model_list:
                # Remove name key if its value is None #16689
                if 'name' in model and model['name'] is None:
                    del model['name']

                if prefix_id:
                    model['id'] = f'{prefix_id}.{model.get("id", model.get("name", ""))}'

                if tags:
                    model['tags'] = tags

                if connection_type:
                    model['connection_type'] = connection_type

                if provider:
                    model['provider'] = provider

    log.debug(f'get_all_models:responses() {responses}')
    return responses


async def get_filtered_models(models, user, db=None):
    # Filter models based on user access control
    model_ids = [model['id'] for model in models.get('data', [])]
    model_infos = {model_info.id: model_info for model_info in await Models.get_models_by_ids(model_ids, db=db)}
    user_group_ids = {group.id for group in await Groups.get_groups_by_member_id(user.id, db=db)}

    # Batch-fetch accessible resource IDs in a single query instead of N has_access calls
    accessible_model_ids = await AccessGrants.get_accessible_resource_ids(
        user_id=user.id,
        resource_type='model',
        resource_ids=list(model_infos.keys()),
        permission='read',
        user_group_ids=user_group_ids,
        db=db,
    )

    filtered_models = []
    for model in models.get('data', []):
        model_info = model_infos.get(model['id'])
        if model_info:
            if user.id == model_info.user_id or model_info.id in accessible_model_ids:
                filtered_models.append(model)
    return filtered_models


@cached(
    ttl=MODELS_CACHE_TTL,
    # key_builder (not key) is the per-call hook in aiocache 0.12; `key=` is a
    # static key, so a `key=lambda` collapsed every caller to one shared entry.
    key_builder=lambda _func, request, user=None: f'openai_all_models_{user.id}' if user else 'openai_all_models',
)
async def get_all_models(request: Request, user: UserModel) -> dict[str, list]:
    log.info('get_all_models()')

    enable_openai_api, api_base_urls, _, api_configs = await get_openai_runtime_config()
    if not enable_openai_api:
        request.app.state.OPENAI_MODELS = {}
        return {'data': []}

    responses = await get_all_models_responses(request, user=user)

    def extract_data(response):
        if response and 'data' in response:
            return response['data']
        if isinstance(response, list):
            return response
        return None

    def is_supported_openai_models(model_id):
        return not any(name in model_id for name in _UNSUPPORTED_OPENAI_MODEL_KEYWORDS)

    def get_merged_models(model_lists):
        log.debug(f'merge_models_lists {model_lists}')
        models = {}

        for idx, model_list in enumerate(model_lists):
            if model_list is not None and 'error' not in model_list:
                base_url = api_base_urls[idx]
                hostname = urlparse(base_url).hostname if base_url else None
                api_config = api_configs.get(str(idx), api_configs.get(base_url, {}))

                for model in model_list:
                    model_id = model.get('id') or model.get('name')

                    if hostname == 'api.openai.com' and not is_supported_openai_models(model_id):
                        # Skip unwanted OpenAI models
                        continue

                    if model_id and model_id not in models:
                        provider = model.get('provider', '')
                        merged = {
                            **model,
                            'name': model.get('name', model_id),
                            'owned_by': 'openai',
                            'openai': model,
                            'connection_type': model.get('connection_type', 'external'),
                            'provider': provider,
                            'urlIdx': idx,
                        }

                        loaded = get_llamacpp_model_loaded_state(
                            model,
                            provider,
                            manual_model_ids=bool(api_config.get('model_ids')),
                        )
                        if loaded is not None:
                            merged['loaded'] = loaded

                        models[model_id] = merged

        return models

    models = get_merged_models(map(extract_data, responses))
    log.debug(f'models: {models}')

    request.app.state.OPENAI_MODELS = models
    return {'data': list(models.values())}


@router.get('/models')
@router.get('/models/{url_idx}')
async def get_models(request: Request, url_idx: int | None = None, user=Depends(get_verified_user)):
    if not await Config.get('openai.enable'):
        raise HTTPException(status_code=503, detail='OpenAI API is disabled')

    models = {
        'data': [],
    }

    if url_idx is None:
        models = await get_all_models(request, user=user)
    else:
        url, key, api_config = await get_openai_connection(url_idx)

        r = None
        async with aiohttp.ClientSession(
            trust_env=True,
            timeout=_MODEL_LIST_TIMEOUT,
        ) as session:
            try:
                headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

                if api_config.get('azure') or api_config.get('provider') == 'azure':
                    models = {
                        'data': api_config.get('model_ids', []) or [],
                        'object': 'list',
                    }
                elif is_anthropic_url(url):
                    models = await get_anthropic_models(url, key, user=user)
                    if models is None:
                        raise Exception('Failed to connect to Anthropic API')
                else:
                    async with session.get(
                        f'{url}/models',
                        headers=headers,
                        cookies=cookies,
                        ssl=AIOHTTP_CLIENT_SESSION_SSL,
                    ) as r:
                        if r.status != 200:
                            error_detail = f'HTTP Error: {r.status}'
                            try:
                                res = await r.json(loads=JSONCodec.loads)
                                if 'error' in res:
                                    error_detail = f'External Error: {res["error"]}'
                            except Exception:
                                pass
                            raise Exception(error_detail)

                        response_data = await r.json(loads=JSONCodec.loads)

                        if 'api.openai.com' in url:
                            response_data['data'] = [
                                model
                                for model in response_data.get('data', [])
                                if not any(name in model['id'] for name in _UNSUPPORTED_OPENAI_MODEL_KEYWORDS)
                            ]

                        models = response_data
            except aiohttp.ClientError as e:
                # ClientError covers all aiohttp requests issues
                log.exception(f'Client error: {str(e)}')
                raise HTTPException(status_code=500, detail='Open WebUI: Server Connection Error')
            except Exception as e:
                log.exception(f'Unexpected error: {e}')
                error_detail = f'Unexpected error: {str(e)}'
                raise HTTPException(status_code=500, detail=error_detail)

    if user.role == 'user' and not BYPASS_MODEL_ACCESS_CONTROL:
        models['data'] = await get_filtered_models(models, user)

    return models


class ConnectionVerificationForm(BaseModel):
    url: str
    key: str

    config: dict | None = None


@router.post('/verify')
async def verify_connection(
    request: Request,
    form_data: ConnectionVerificationForm,
    user=Depends(get_admin_user),
):
    url = form_data.url
    key = form_data.key

    api_config = form_data.config or {}

    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=_MODEL_LIST_TIMEOUT,
    ) as session:
        try:
            headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

            if api_config.get('azure') or api_config.get('provider') == 'azure':
                # Only set api-key header if not using Azure Entra ID authentication
                auth_type = api_config.get('auth_type', 'bearer')
                if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                    headers['api-key'] = key

                # Azure v1 format: base URL already ends with /openai/v1,
                # use standard /models endpoint without api-version.
                is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

                if is_azure_v1:
                    verify_url = f'{url.rstrip("/")}/models'
                else:
                    api_version = api_config.get('api_version', '') or '2023-03-15-preview'
                    verify_url = f'{url}/openai/models?api-version={api_version}'

                async with session.get(
                    url=verify_url,
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    try:
                        response_data = await r.json(loads=JSONCodec.loads)
                    except Exception:
                        response_data = await r.text()

                    if r.status != 200:
                        if isinstance(response_data, (dict, list)):
                            return JSONResponse(status_code=r.status, content=response_data)
                        else:
                            return PlainTextResponse(status_code=r.status, content=response_data)

                    return response_data
            elif is_anthropic_url(url):
                result = await get_anthropic_models(url, key)
                if result is None:
                    raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)
                if 'error' in result:
                    raise HTTPException(status_code=500, detail=result['error'])
                return result
            else:
                async with session.get(
                    f'{url}/models',
                    headers=headers,
                    cookies=cookies,
                    ssl=AIOHTTP_CLIENT_SESSION_SSL,
                ) as r:
                    try:
                        response_data = await r.json(loads=JSONCodec.loads)
                    except Exception:
                        response_data = await r.text()

                    if r.status != 200:
                        if isinstance(response_data, (dict, list)):
                            return JSONResponse(status_code=r.status, content=response_data)
                        else:
                            return PlainTextResponse(status_code=r.status, content=response_data)

                    return response_data

        except aiohttp.ClientError as e:
            # ClientError covers all aiohttp requests issues
            log.exception(f'Client error: {str(e)}')
            raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)
        except Exception as e:
            log.exception(f'Unexpected error: {e}')
            raise HTTPException(status_code=500, detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR)


def get_azure_allowed_params(api_version: str) -> set[str]:
    allowed_params = {
        'messages',
        'temperature',
        'role',
        'content',
        'contentPart',
        'contentPartImage',
        'enhancements',
        'dataSources',
        'n',
        'stream',
        'stop',
        'max_tokens',
        'presence_penalty',
        'frequency_penalty',
        'logit_bias',
        'user',
        'function_call',
        'functions',
        'tools',
        'tool_choice',
        'top_p',
        'log_probs',
        'top_logprobs',
        'response_format',
        'seed',
        'max_completion_tokens',
        'reasoning_effort',
    }

    try:
        if api_version >= '2024-09-01-preview':
            allowed_params.add('stream_options')
    except ValueError:
        log.debug(f'Invalid API version {api_version} for Azure OpenAI. Defaulting to allowed parameters.')

    return allowed_params


def is_openai_new_model(model: str) -> bool:
    model_lower = model.lower()
    # o-series models (o1, o3, o4, o5, ...)
    if re.match(r'^o\d+', model_lower):
        return True
    # gpt-N where N >= 5 (gpt-5, gpt-5.2, gpt-6, ...)
    m = re.match(r'^gpt-(\d+)', model_lower)
    if m and int(m.group(1)) >= 5:
        return True
    return False


def _sanitize_model_for_url(model: str) -> str:
    """Sanitize a model name before interpolating it into a URL path.

    Rejects path traversal attempts (../, /, \\) and percent-encodes
    the name so it is safe to use as a single URL path segment
    (e.g. Azure deployment name).
    """
    if not model or '..' in model or '/' in model or '\\' in model:
        raise HTTPException(
            status_code=400,
            detail='Invalid model name: must not be empty or contain path separators or traversal sequences',
        )
    return quote(model, safe='')


def convert_to_azure_payload(url, payload: dict, api_version: str):
    model = payload.get('model', '')

    # Filter allowed parameters based on Azure OpenAI API
    allowed_params = get_azure_allowed_params(api_version)

    # Special handling for o-series models
    if is_openai_new_model(model):
        # Convert max_tokens to max_completion_tokens for o-series models
        if 'max_tokens' in payload:
            payload['max_completion_tokens'] = payload['max_tokens']
            del payload['max_tokens']

        # Remove temperature if not 1 for o-series models
        if 'temperature' in payload and payload['temperature'] != 1:
            log.debug(
                f'Removing temperature parameter for o-series model {model} as only default value (1) is supported'
            )
            del payload['temperature']

    # Filter out unsupported parameters
    payload = {k: v for k, v in payload.items() if k in allowed_params}

    # Sanitize model name to prevent path traversal in the deployment URL
    model = _sanitize_model_for_url(model)

    url = f'{url}/openai/deployments/{model}'
    return url, payload


# Fields accepted by the Responses API for each input item type.
RESPONSES_ALLOWED_FIELDS: dict[str, set[str]] = {
    'message': {'type', 'role', 'content'},
    'function_call': {'type', 'call_id', 'name', 'arguments', 'id'},
    'function_call_output': {'type', 'call_id', 'output'},
}


def _normalize_stored_item(item: dict) -> dict:
    """Strip local-only fields from a stored output item before replaying it.

    Open WebUI stores extra bookkeeping fields (``id``, ``status``,
    ``started_at``, ``ended_at``, ``duration``, ``_tag_type``,
    ``attributes``, ``summary``, etc.) that the Responses API does
    not accept.  This helper returns a copy containing only the
    fields the API understands.
    """
    item_type = item.get('type', '')
    allowed = RESPONSES_ALLOWED_FIELDS.get(item_type)
    if allowed is None:
        # Unknown type — pass through as-is (e.g. reasoning, extension items).
        return item
    return {k: v for k, v in item.items() if k in allowed}


def convert_to_responses_payload(payload: dict) -> dict:
    """
    Convert Chat Completions payload to Responses API format.

    Chat Completions: { messages: [{role, content}], ... }
    Responses API: { input: [{type: "message", role, content: [...]}], instructions: "system" }
    """
    messages = payload.pop('messages', [])

    system_content = ''
    input_items = []

    for msg in messages:
        role = msg.get('role', 'user')
        content = msg.get('content', '')

        # Check for stored output items (from previous Responses API turn)
        stored_output = msg.get('output')
        if stored_output and isinstance(stored_output, list):
            input_items.extend(_normalize_stored_item(item) for item in stored_output)
            continue

        if role == 'system':
            if isinstance(content, str):
                system_content = content
            elif isinstance(content, list):
                system_content = '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
            continue

        # Handle assistant messages with tool_calls (from convert_output_to_messages)
        if role == 'assistant' and msg.get('tool_calls'):
            # Add text content as message if present
            if content:
                text = (
                    content
                    if isinstance(content, str)
                    else '\n'.join(p.get('text', '') for p in content if p.get('type') == 'text')
                )
                if text.strip():
                    input_items.append(
                        {
                            'type': 'message',
                            'role': 'assistant',
                            'content': [{'type': 'output_text', 'text': text}],
                        }
                    )
            # Convert each tool_call to a function_call input item
            for tool_call in msg['tool_calls']:
                func = tool_call.get('function', {})
                input_items.append(
                    {
                        'type': 'function_call',
                        'call_id': tool_call.get('id', ''),
                        'name': func.get('name', ''),
                        'arguments': func.get('arguments', '{}'),
                    }
                )
            continue

        # Handle tool result messages
        if role == 'tool':
            input_items.append(
                {
                    'type': 'function_call_output',
                    'call_id': msg.get('tool_call_id', ''),
                    'output': msg.get('content', ''),
                }
            )
            continue

        # Convert content format
        text_type = 'output_text' if role == 'assistant' else 'input_text'

        if isinstance(content, str):
            content_parts = [{'type': text_type, 'text': content}]
        elif isinstance(content, list):
            content_parts = []
            for part in content:
                if part.get('type') == 'text':
                    content_parts.append({'type': text_type, 'text': part.get('text', '')})
                elif part.get('type') == 'image_url':
                    url_data = part.get('image_url', {})
                    url = url_data.get('url', '') if isinstance(url_data, dict) else url_data
                    content_parts.append({'type': 'input_image', 'image_url': url})
        else:
            content_parts = [{'type': text_type, 'text': str(content)}]

        input_items.append({'type': 'message', 'role': role, 'content': content_parts})

    responses_payload = {**payload, 'input': input_items}

    # Forward previous_response_id when the middleware has set it
    # (only used when ENABLE_RESPONSES_API_STATEFUL is enabled).
    previous_response_id = responses_payload.pop('previous_response_id', None)
    if previous_response_id:
        responses_payload['previous_response_id'] = previous_response_id

    if system_content:
        responses_payload['instructions'] = system_content

    if 'max_tokens' in responses_payload:
        responses_payload['max_output_tokens'] = responses_payload.pop('max_tokens')

    if 'max_completion_tokens' in responses_payload:
        responses_payload['max_output_tokens'] = responses_payload.pop('max_completion_tokens')

    # Remove Chat Completions-only parameters not supported by the Responses API
    for unsupported_key in (
        'stream_options',
        'logit_bias',
        'frequency_penalty',
        'presence_penalty',
        'stop',
    ):
        responses_payload.pop(unsupported_key, None)

    # Convert Chat Completions tools format to Responses API format
    # Chat Completions: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    # Responses API:    {"type": "function", "name": ..., "description": ..., "parameters": ...}
    if 'tools' in responses_payload and isinstance(responses_payload['tools'], list):
        converted_tools = []
        for tool in responses_payload['tools']:
            if isinstance(tool, dict) and 'function' in tool:
                func = tool['function']
                converted_tool = {'type': tool.get('type', 'function')}
                if isinstance(func, dict):
                    converted_tool['name'] = func.get('name', '')
                    if 'description' in func:
                        converted_tool['description'] = func['description']
                    if 'parameters' in func:
                        converted_tool['parameters'] = func['parameters']
                    if 'strict' in func:
                        converted_tool['strict'] = func['strict']
                converted_tools.append(converted_tool)
            else:
                # Already in correct format or unknown format, pass through
                converted_tools.append(tool)
        responses_payload['tools'] = converted_tools

    return responses_payload


def convert_responses_result(response: dict) -> dict:
    """
    Convert non-streaming Responses API result to Chat Completions format.

    Extracts text from message output items so all downstream consumers
    (frontend tasks, get_content_from_response) work without modification.
    """
    output_items = response.get('output', [])

    content = ''
    for item in output_items:
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'output_text':
                    content += part.get('text', '')

    return {
        'id': response.get('id', ''),
        'object': 'chat.completion',
        'model': response.get('model', ''),
        'choices': [
            {
                'index': 0,
                'message': {
                    'role': 'assistant',
                    'content': content,
                },
                'finish_reason': 'stop',
            }
        ],
        'usage': response.get('usage', {}),
    }


@router.post('/chat/completions')
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
):
    if not await Config.get('openai.enable'):
        raise HTTPException(status_code=503, detail='OpenAI API is disabled')

    # NOTE: We intentionally do NOT use Depends(get_async_session) here.
    # Database operations (get_model_by_id, AccessGrants.has_access) manage their own short-lived sessions.
    # This prevents holding a connection during the entire LLM call (30-60+ seconds),
    # which would exhaust the connection pool under concurrent load.

    # bypass_filter and bypass_system_prompt are read from request.state to prevent
    # external clients from setting them via query parameter. Only internal
    # server-side callers (e.g. utils/chat.py) should set
    # request.state.bypass_filter / request.state.bypass_system_prompt = True.
    bypass_filter = getattr(request.state, 'bypass_filter', False)
    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True
    bypass_system_prompt = getattr(request.state, 'bypass_system_prompt', False)

    idx = 0

    payload = {**form_data}
    metadata = payload.pop('metadata', None)

    model_id = form_data.get('model')
    model_info = await Models.get_model_by_id(model_id)

    # Check model info and override the payload
    if model_info:
        if model_info.base_model_id:
            base_model_id = (
                request.base_model_id if hasattr(request, 'base_model_id') else model_info.base_model_id
            )  # Use request's base_model_id if available
            payload['model'] = base_model_id
            model_id = base_model_id

        params = model_info.params.model_dump()

        if params:
            system = params.pop('system', None)

            payload = apply_model_params_to_body_openai(params, payload)
            if not bypass_system_prompt:
                payload = await apply_system_prompt_to_body(system, payload, metadata, user)

        await check_model_access(user, model_info, bypass_filter)
    else:
        await check_model_access(user, None, bypass_filter)

    # Check if model is already in app state cache to avoid expensive get_all_models() call
    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS
    model = models.get(model_id)

    if model:
        idx = model['urlIdx']
    else:
        raise HTTPException(
            status_code=404,
            detail=ERROR_MESSAGES.MODEL_NOT_FOUND(),
        )

    url, key, api_config = await get_openai_connection(idx)

    prefix_id = api_config.get('prefix_id', None)
    payload['model'] = strip_provider_model_prefix(payload['model'], prefix_id)

    attached_file_context = await _heruvim_attached_file_context(metadata, user)
    if attached_file_context:
        _inject_system_context(payload, attached_file_context)

    if (
        api_config.get('provider') == 'openai'
        and HERUVIM_RAGFLOW_AUTO_RETRIEVAL
        and HERUVIM_RAGFLOW_ENABLED
        and not HERUVIM_LLM_ENABLED
        and not attached_file_context
    ):
        document_query = _extract_latest_user_text(payload.get('messages'))
        metadata_task = metadata.get('task') if isinstance(metadata, dict) else None
        if not metadata_task and _should_use_heruvim_ragflow(document_query):
            try:
                retrieval = await _heruvim_ragflow_retrieve(document_query)
            except Exception as exc:
                log.exception('HERUVIM RAGFlow auto-retrieval failed')
                retrieval = {'ok': False, 'error': str(exc)}
            public_base_url = await _heruvim_public_base_url(request)
            _inject_system_context(
                payload,
                _format_ragflow_context(document_query, retrieval, public_base_url=public_base_url),
            )

    # RAGFlow returns its source references only when explicitly requested.
    # Preserve caller-supplied filters while enforcing references for ХЕРУВИМ.
    if api_config.get('provider') == 'ragflow' and api_config.get('ragflow_references', True):
        extra_body = payload.get('extra_body')
        if not isinstance(extra_body, dict):
            extra_body = {}
        reference_metadata = extra_body.get('reference_metadata')
        if not isinstance(reference_metadata, dict):
            reference_metadata = {}
        payload['extra_body'] = {
            **extra_body,
            'reference': True,
            'reference_metadata': {
                **reference_metadata,
                'include': True,
            },
        }

        # This is a generation-time evidence gate, not a cosmetic citation
        # toggle. It forces the model to classify every material statement as
        # sourced, inferred, or unsupported before returning it to the user.
        metadata_task = metadata.get('task') if isinstance(metadata, dict) else None
        if HERUVIM_REQUIRE_SOURCE_VERIFICATION and not metadata_task:
            source_guard = (
                'ОБЯЗАТЕЛЬНЫЙ КОНТРОЛЬ ИСТОЧНИКОВ: перед выдачей ответа проверь каждое '
                'существенное фактическое утверждение по найденным фрагментам. Сразу после '
                'подтверждённого утверждения поставь ссылку на соответствующий источник в '
                'формате, поддерживаемом контекстом. Расчёты помечай как «Расчёт», выводы — '
                'как «Вывод». Если утверждение не подтверждается источником, не выдавай его '
                'за факт: пометь «Требует проверки» и укажи, каких данных не хватает. '
                'Не создавай ссылок и не приписывай источнику сведений, которых в нём нет.'
            )
            messages = payload.get('messages')
            if isinstance(messages, list):
                system_message = next(
                    (message for message in messages if message.get('role') in {'system', 'developer'}),
                    None,
                )
                if system_message and isinstance(system_message.get('content'), str):
                    if source_guard not in system_message['content']:
                        system_message['content'] = f"{system_message['content']}\n\n{source_guard}"
                else:
                    messages.insert(0, {'role': 'system', 'content': source_guard})

    # Add user info to the payload if the model is a pipeline
    if 'pipeline' in model and model.get('pipeline'):
        payload['user'] = {
            'name': user.name,
            'id': user.id,
            'email': user.email,
            'role': user.role,
        }

    # Check if model is a reasoning model that needs special handling
    if is_openai_new_model(payload['model']):
        payload = openai_reasoning_model_handler(payload)
    elif 'api.openai.com' not in url:
        # Remove "max_completion_tokens" from the payload for backward compatibility
        if 'max_completion_tokens' in payload:
            payload['max_tokens'] = payload['max_completion_tokens']
            del payload['max_completion_tokens']

    if 'max_tokens' in payload and 'max_completion_tokens' in payload:
        del payload['max_tokens']

    # Convert the modified body back to JSON
    if 'logit_bias' in payload and payload['logit_bias']:
        logit_bias = convert_logit_bias_input_to_json(payload['logit_bias'])

        if logit_bias:
            payload['logit_bias'] = json.loads(logit_bias)

    headers, cookies = await get_headers_and_cookies(request, url, key, api_config, metadata, user=user)

    is_responses = api_config.get('api_type') == 'responses'

    if api_config.get('azure') or api_config.get('provider') == 'azure':
        # Only set api-key header if not using Azure Entra ID authentication
        auth_type = api_config.get('auth_type', 'bearer')
        if auth_type not in ('azure_ad', 'microsoft_entra_id'):
            headers['api-key'] = key

        # Azure v1 format: base URL already ends with /openai/v1,
        # model stays in the payload, no deployment URL rewriting.
        is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

        if is_azure_v1:
            if is_responses:
                payload = convert_to_responses_payload(payload)
                request_url = f'{url.rstrip("/")}/responses'
            else:
                request_url = f'{url.rstrip("/")}/chat/completions'
        else:
            api_version = api_config.get('api_version', '2023-03-15-preview')
            request_url, payload = convert_to_azure_payload(url, payload, api_version)
            headers['api-version'] = api_version

            if is_responses:
                payload = convert_to_responses_payload(payload)
                request_url = f'{request_url}/responses?api-version={api_version}'
            else:
                request_url = f'{request_url}/chat/completions?api-version={api_version}'
    else:
        if is_responses:
            payload = convert_to_responses_payload(payload)
            request_url = f'{url}/responses'
        else:
            request_url = f'{url}/chat/completions'
    requested_model = payload.get('model')

    metadata_task = metadata.get('task') if isinstance(metadata, dict) else None
    if (
        not is_responses
        and api_config.get('provider') == 'openai'
        and not metadata_task
        and (bool(attached_file_context) or HERUVIM_RAGFLOW_ENABLED or HERUVIM_LLM_ENABLED)
    ):
        payload = await _apply_heruvim_ragflow_tool_loop(
            request,
            payload=payload,
            request_url=request_url,
            headers=headers,
            cookies=cookies,
            provider_url=url,
            api_key=key,
            requested_model=requested_model,
            user=user,
        )

    # For Chat Completions, strip image parts from multimodal tool messages
    # (Chat Completions doesn't support images in tool content).
    if not is_responses and 'messages' in payload:
        for message in payload['messages']:
            if message.get('role') == 'tool' and isinstance(message.get('content'), list):
                message['content'] = ''.join(
                    part.get('text', '') for part in message['content'] if part.get('type') in ('input_text', 'text')
                )

    is_streaming_request = bool(payload.get('stream', False))
    if not is_streaming_request:
        payload.pop('stream_options', None)

    payload = json.dumps(payload)

    r = None
    streaming = False
    response = None

    try:
        session = await get_session()

        r = await session.request(
            method='POST',
            url=request_url,
            data=payload,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=is_streaming_request),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            # If the provider returned an error status with SSE content-type,
            # read the body and return a proper error response instead of
            # streaming the error back (which hides the error from logs).
            if r.status >= 400:
                error_body = await r.text()
                log.error(
                    'Provider returned HTTP %d with SSE content-type: %s',
                    r.status,
                    error_body[:1000],
                )
                try:
                    error_json = json.loads(error_body)
                    await publish_model_provider_request_failed(
                        request,
                        actor=user,
                        provider='openai-compatible',
                        base_url=url,
                        api_key=key,
                        status=r.status,
                        requested_model=requested_model,
                        upstream_error=error_json,
                    )
                    return JSONResponse(status_code=r.status, content=error_json)
                except json.JSONDecodeError:
                    await publish_model_provider_request_failed(
                        request,
                        actor=user,
                        provider='openai-compatible',
                        base_url=url,
                        api_key=key,
                        status=r.status,
                        requested_model=requested_model,
                        upstream_error=error_body,
                    )
                    return JSONResponse(
                        status_code=r.status,
                        content={'error': {'message': error_body, 'code': r.status}},
                    )

            streaming = True
            return StreamingResponse(
                stream_wrapper(r, content_handler=stream_chunks_handler),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response = await r.json(loads=JSONCodec.loads)
            except Exception as e:
                log.error(e)
                response = await r.text()

            if r.status >= 400:
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=url,
                    api_key=key,
                    status=r.status,
                    requested_model=requested_model,
                    upstream_error=response,
                )
                if isinstance(response, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response)
                else:
                    return PlainTextResponse(status_code=r.status, content=response)

            # Convert Responses API result to simple format
            if is_responses and isinstance(response, dict):
                response = convert_responses_result(response)

            return response
    except Exception as e:
        log.exception(e)

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


async def embeddings(request: Request, form_data: dict, user):
    """
    Calls the embeddings endpoint for OpenAI-compatible providers.

    Args:
        request (Request): The FastAPI request context.
        form_data (dict): OpenAI-compatible embeddings payload.
        user (UserModel): The authenticated user.

    Returns:
        dict: OpenAI-compatible embeddings response.
    """
    idx = 0
    # Prepare payload/body
    body = json.dumps(form_data)
    # Find correct backend url/key based on model
    model_id = form_data.get('model')
    # Check if model is already in app state cache to avoid expensive get_all_models() call
    models = request.app.state.OPENAI_MODELS
    if not models or model_id not in models:
        await get_all_models(request, user=user)
        models = request.app.state.OPENAI_MODELS
    if model_id in models:
        idx = models[model_id]['urlIdx']

    url, key, api_config = await get_openai_connection(idx)

    r = None
    streaming = False

    headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

    if api_config.get('azure') or api_config.get('provider') == 'azure':
        # Only set api-key header if not using Azure Entra ID authentication
        auth_type = api_config.get('auth_type', 'bearer')
        if auth_type not in ('azure_ad', 'microsoft_entra_id'):
            headers['api-key'] = key

        # Azure v1 format: base URL already ends with /openai/v1,
        # model stays in the payload, no deployment URL rewriting.
        is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

        if is_azure_v1:
            embeddings_url = f'{url.rstrip("/")}/embeddings'
        else:
            api_version = api_config.get('api_version', '2023-03-15-preview')
            model = _sanitize_model_for_url(form_data.get('model', ''))
            embeddings_url = f'{url}/openai/deployments/{model}/embeddings?api-version={api_version}'
            headers['api-version'] = api_version
    else:
        embeddings_url = f'{url}/embeddings'
    requested_model = form_data.get('model')

    try:
        session = await get_session()
        r = await session.request(
            method='POST',
            url=embeddings_url,
            data=body,
            headers=headers,
            cookies=cookies,
            timeout=get_client_timeout(),
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        )

        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r, passthrough=True),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json(loads=JSONCodec.loads)
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=url,
                    api_key=key,
                    status=r.status,
                    requested_model=requested_model,
                    upstream_error=response_data,
                )
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


class ResponsesForm(BaseModel):
    model_config = ConfigDict(extra='allow')

    model: str
    input: list | str | None = None
    instructions: str | None = None
    stream: bool | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    top_p: float | None = None
    tools: list | None = None
    tool_choice: str | dict | None = None
    text: dict | None = None
    truncation: str | None = None
    metadata: dict | None = None
    store: bool | None = None
    reasoning: dict | None = None
    previous_response_id: str | None = None


@router.post('/responses')
async def responses(
    request: Request,
    form_data: ResponsesForm,
    user=Depends(get_verified_user),
):
    """
    Forward requests to the OpenAI Responses API endpoint.
    Routes to the correct upstream backend based on the model field.
    """
    payload = form_data.model_dump(exclude_none=True)
    is_streaming_request = bool(payload.get('stream', False))

    idx = 0
    model_id = form_data.model

    # Enforce per-model access control
    await check_model_access(user, await Models.get_model_by_id(model_id), BYPASS_MODEL_ACCESS_CONTROL)

    body = json.dumps(payload)

    if model_id:
        models = request.app.state.OPENAI_MODELS
        if not models or model_id not in models:
            await get_all_models(request, user=user)
            models = request.app.state.OPENAI_MODELS
        if model_id in models:
            idx = models[model_id]['urlIdx']

    url, key, api_config = await get_openai_connection(idx)

    r = None
    streaming = False

    try:
        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        if api_config.get('azure') or api_config.get('provider') == 'azure':
            auth_type = api_config.get('auth_type', 'bearer')
            if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                headers['api-key'] = key

            is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

            if is_azure_v1:
                request_url = f'{url.rstrip("/")}/responses'
            else:
                api_version = api_config.get('api_version', '2023-03-15-preview')
                headers['api-version'] = api_version
                model = _sanitize_model_for_url(payload.get('model', ''))
                request_url = f'{url}/openai/deployments/{model}/responses?api-version={api_version}'
        else:
            request_url = f'{url}/responses'

        session = await get_session()
        r = await session.request(
            method='POST',
            url=request_url,
            data=body,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=is_streaming_request),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r, passthrough=True),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json(loads=JSONCodec.loads)
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=url,
                    api_key=key,
                    status=r.status,
                    requested_model=payload.get('model'),
                    upstream_error=response_data,
                )
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data

    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=ERROR_MESSAGES.SERVER_CONNECTION_ERROR,
        )
    finally:
        if not streaming:
            await cleanup_response(r)


@router.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    """
    Deprecated: proxy all requests to OpenAI API.
    Disabled by default. Set ENABLE_OPENAI_API_PASSTHROUGH=True to enable.
    """

    if not ENABLE_OPENAI_API_PASSTHROUGH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Direct API passthrough is disabled. Set ENABLE_OPENAI_API_PASSTHROUGH=True to enable.',
        )

    body = await request.body()

    # Parse JSON body to resolve model-based routing
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            payload = None
    is_streaming_request = bool(payload.get('stream', False)) if isinstance(payload, dict) else False

    idx = 0
    model_id = payload.get('model') if isinstance(payload, dict) else None
    if model_id:
        models = request.app.state.OPENAI_MODELS
        if not models or model_id not in models:
            await get_all_models(request, user=user)
            models = request.app.state.OPENAI_MODELS
        if model_id in models:
            idx = models[model_id]['urlIdx']

    url, key, api_config = await get_openai_connection(idx)
    base_url = url

    r = None
    streaming = False

    try:
        headers, cookies = await get_headers_and_cookies(request, url, key, api_config, user=user)

        if api_config.get('azure') or api_config.get('provider') == 'azure':
            # Only set api-key header if not using Azure Entra ID authentication
            auth_type = api_config.get('auth_type', 'bearer')
            if auth_type not in ('azure_ad', 'microsoft_entra_id'):
                headers['api-key'] = key

            is_azure_v1 = bool(re.search(r'/openai/v1(?:/|$)', url))

            if is_azure_v1:
                qs = request.url.query
                request_url = f'{url.rstrip("/")}/{path}' + (f'?{qs}' if qs else '')
            else:
                api_version = api_config.get('api_version', '2023-03-15-preview')
                headers['api-version'] = api_version

                payload = json.loads(body)
                url, payload = convert_to_azure_payload(url, payload, api_version)
                body = json.dumps(payload).encode()

                request_url = f'{url}/{path}?api-version={api_version}'
        else:
            request_url = f'{url}/{path}'

        session = await get_session()
        r = await session.request(
            method=request.method,
            url=request_url,
            data=body,
            headers=headers,
            cookies=cookies,
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
            timeout=get_client_timeout(stream=is_streaming_request),
        )

        # Check if response is SSE
        if 'text/event-stream' in r.headers.get('Content-Type', ''):
            streaming = True
            return StreamingResponse(
                stream_wrapper(r, passthrough=True),
                status_code=r.status,
                headers=_clean_proxy_headers(r.headers),
            )
        else:
            try:
                response_data = await r.json(loads=JSONCodec.loads)
            except Exception:
                response_data = await r.text()

            if r.status >= 400:
                await publish_model_provider_request_failed(
                    request,
                    actor=user,
                    provider='openai-compatible',
                    base_url=base_url,
                    api_key=key,
                    status=r.status,
                    requested_model=model_id,
                    upstream_error=response_data,
                )
                if isinstance(response_data, (dict, list)):
                    return JSONResponse(status_code=r.status, content=response_data)
                else:
                    return PlainTextResponse(status_code=r.status, content=response_data)

            return response_data

    except HTTPException:
        raise
    except Exception as e:
        log.exception(e)
        raise HTTPException(
            status_code=r.status if r else 500,
            detail='Open WebUI: Server Connection Error',
        )
    finally:
        if not streaming:
            await cleanup_response(r)
