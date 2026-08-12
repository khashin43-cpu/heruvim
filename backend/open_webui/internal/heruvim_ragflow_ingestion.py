"""Persistent Open WebUI attachment synchronization with RAGFlow datasets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from open_webui.config import (
    HERUVIM_RAGFLOW_API_KEY,
    HERUVIM_RAGFLOW_BASE_URL,
    HERUVIM_RAGFLOW_CHAT_ID,
    HERUVIM_RAGFLOW_DATASET_IDS,
    HERUVIM_RAGFLOW_ENABLED,
    HERUVIM_RAGFLOW_SYNC_ATTACHMENTS,
    HERUVIM_RAGFLOW_SYNC_POLL_SECONDS,
    HERUVIM_RAGFLOW_SYNC_TIMEOUT_SECONDS,
    HERUVIM_RAGFLOW_SYNC_WORKERS,
)
from open_webui.models.files import FileModel, Files
from open_webui.storage.provider import Storage


log = logging.getLogger(__name__)

TERMINAL_STATES = {'ready', 'failed', 'cancelled'}
RAGFLOW_RUN_STATES = {
    '0': 'queued',
    '1': 'indexing',
    '2': 'cancelled',
    '3': 'ready',
    '4': 'failed',
    '5': 'queued',
    'UNSTART': 'queued',
    'RUNNING': 'indexing',
    'CANCEL': 'cancelled',
    'DONE': 'ready',
    'FAIL': 'failed',
    'SCHEDULE': 'queued',
}


def canonical_document_id(file: FileModel) -> str:
    """Return a stable cross-system identifier derived from the file content."""
    meta = file.meta or {}
    digest = str(meta.get('file_hash') or file.hash or '').strip().lower()
    if len(digest) != 64:
        digest = hashlib.sha256(file.id.encode('utf-8')).hexdigest()
    return f'heruvim:{digest}'


def public_ingestion_record(file: FileModel) -> dict[str, Any]:
    sync = dict((file.data or {}).get('ragflow_sync') or {})
    return {
        'file_id': file.id,
        'filename': file.filename,
        'canonical_document_id': sync.get('canonical_document_id') or canonical_document_id(file),
        'state': sync.get('state', 'not_queued'),
        'progress': sync.get('progress', 0),
        'message': sync.get('message', ''),
        'error': sync.get('error'),
        'attempt': sync.get('attempt', 0),
        'datasets': sync.get('datasets', []),
        'queued_at': sync.get('queued_at'),
        'updated_at': sync.get('updated_at') or file.updated_at,
        'completed_at': sync.get('completed_at'),
    }


class RAGFlowIngestionQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, str, bool]] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._queued_ids: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(HERUVIM_RAGFLOW_ENABLED and HERUVIM_RAGFLOW_SYNC_ATTACHMENTS)

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if not self.enabled or self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(index), name=f'heruvim-ragflow-ingestion-{index}')
            for index in range(HERUVIM_RAGFLOW_SYNC_WORKERS)
        ]
        for file in await Files.get_files():
            state = ((file.data or {}).get('ragflow_sync') or {}).get('state')
            if state in {'queued', 'uploading', 'indexing', 'checking'}:
                await self.enqueue(file.id, file.user_id, preserve_status=True)
        log.info('HERUVIM RAGFlow ingestion queue started with %d worker(s)', len(self._workers))

    async def stop(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._queued_ids.clear()

    async def enqueue(
        self,
        file_id: str,
        user_id: str,
        *,
        force: bool = False,
        preserve_status: bool = False,
    ) -> bool:
        if not self.enabled or not file_id or file_id in self._queued_ids:
            return False
        file = await Files.get_file_by_id(file_id)
        if not file or file.user_id != user_id:
            return False
        if not preserve_status:
            previous = (file.data or {}).get('ragflow_sync') or {}
            await self._update(
                file,
                state='queued',
                progress=0,
                message='Ожидает отправки в RAGFlow',
                error=None,
                attempt=int(previous.get('attempt') or 0) + 1,
                queued_at=int(time.time()),
                completed_at=None,
            )
        self._queued_ids.add(file_id)
        await self._queue.put((file_id, user_id, force))
        return True

    async def _worker(self, index: int) -> None:
        while True:
            file_id, user_id, force = await self._queue.get()
            try:
                await self._sync(file_id, user_id, force=force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception('RAGFlow ingestion failed for Open WebUI file %s', file_id)
                file = await Files.get_file_by_id(file_id)
                if file:
                    await self._update(
                        file,
                        state='failed',
                        message='Синхронизация завершилась с ошибкой',
                        error=str(exc)[:2000],
                        completed_at=int(time.time()),
                    )
            finally:
                self._queued_ids.discard(file_id)
                self._queue.task_done()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        files: dict | None = None,
        params: dict | None = None,
        timeout: float = 90,
    ) -> Any:
        headers = {'Authorization': f'Bearer {HERUVIM_RAGFLOW_API_KEY}'}
        async with httpx.AsyncClient(base_url=HERUVIM_RAGFLOW_BASE_URL, headers=headers, timeout=timeout) as client:
            response = await client.request(method, path, json=json_body, files=files, params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get('code') not in (None, 0):
            raise RuntimeError(payload.get('message') or f'RAGFlow error code {payload.get("code")}')
        return payload.get('data') if isinstance(payload, dict) and 'data' in payload else payload

    async def _dataset_ids(self) -> list[str]:
        if HERUVIM_RAGFLOW_DATASET_IDS:
            return list(dict.fromkeys(HERUVIM_RAGFLOW_DATASET_IDS))
        data = await self._request('GET', f'/api/v1/chats/{HERUVIM_RAGFLOW_CHAT_ID}')
        dataset_ids = data.get('dataset_ids', []) if isinstance(data, dict) else []
        dataset_ids = [str(value).strip() for value in dataset_ids if str(value).strip()]
        if not dataset_ids:
            raise RuntimeError('У RAGFlow chat assistant не подключён ни один dataset')
        return list(dict.fromkeys(dataset_ids))

    async def _find_existing(self, dataset_id: str, canonical_id: str) -> dict | None:
        data = await self._request(
            'GET',
            f'/api/v1/datasets/{dataset_id}/documents',
            params={
                'page': 1,
                'page_size': 10,
                'metadata': json.dumps({'heruvim_document_id': canonical_id}, ensure_ascii=False),
            },
        )
        docs = data.get('docs', []) if isinstance(data, dict) else []
        return docs[0] if docs else None

    async def _upload(self, file: FileModel, dataset_id: str, canonical_id: str) -> dict:
        existing = await self._find_existing(dataset_id, canonical_id)
        if existing:
            return existing
        local_path = await asyncio.to_thread(Storage.get_file, file.path)
        contents = await asyncio.to_thread(Path(local_path).read_bytes)
        content_type = str((file.meta or {}).get('content_type') or 'application/octet-stream')
        data = await self._request(
            'POST',
            f'/api/v1/datasets/{dataset_id}/documents',
            files={'file': (file.filename, contents, content_type)},
            timeout=300,
        )
        docs = data if isinstance(data, list) else []
        if not docs:
            raise RuntimeError('RAGFlow не вернул созданный документ')
        document = docs[0]
        document_id = document.get('id')
        await self._request(
            'PATCH',
            f'/api/v1/datasets/{dataset_id}/documents/{document_id}',
            json_body={
                'meta_fields': {
                    'heruvim_document_id': canonical_id,
                    'open_webui_file_id': file.id,
                    'sha256': canonical_id.removeprefix('heruvim:'),
                }
            },
        )
        return document

    async def _document_status(self, dataset_id: str, document_id: str) -> dict:
        data = await self._request(
            'GET',
            f'/api/v1/datasets/{dataset_id}/documents',
            params={'id': document_id, 'page': 1, 'page_size': 1},
        )
        docs = data.get('docs', []) if isinstance(data, dict) else []
        if not docs:
            raise RuntimeError(f'Документ {document_id} исчез из dataset {dataset_id}')
        return docs[0]

    async def _sync(self, file_id: str, user_id: str, *, force: bool) -> None:
        file = await Files.get_file_by_id(file_id)
        if not file or file.user_id != user_id:
            return
        canonical_id = canonical_document_id(file)
        if not file.path:
            raise RuntimeError('У файла Open WebUI отсутствует путь к исходному содержимому')
        previous = (file.data or {}).get('ragflow_sync') or {}
        mappings = {
            item.get('dataset_id'): dict(item)
            for item in previous.get('datasets', [])
            if isinstance(item, dict) and item.get('dataset_id')
        }
        dataset_ids = await self._dataset_ids()

        await self._update(
            file,
            canonical_document_id=canonical_id,
            state='uploading',
            progress=5,
            message='Отправка документа в RAGFlow',
            error=None,
        )

        for offset, dataset_id in enumerate(dataset_ids):
            mapping = mappings.get(dataset_id)
            if not mapping:
                document = await self._upload(file, dataset_id, canonical_id)
                mapping = {
                    'dataset_id': dataset_id,
                    'document_id': document.get('id'),
                    'state': 'queued',
                    'progress': 0,
                }
                mappings[dataset_id] = mapping
            document_id = mapping.get('document_id')
            if not document_id:
                raise RuntimeError(f'RAGFlow не вернул document id для dataset {dataset_id}')

            status = await self._document_status(dataset_id, document_id)
            state = RAGFLOW_RUN_STATES.get(str(status.get('run', '')).upper(), 'queued')
            if force or state in {'queued', 'failed', 'cancelled'}:
                await self._request(
                    'POST',
                    f'/api/v1/datasets/{dataset_id}/documents/parse',
                    json_body={'document_ids': [document_id]},
                )
                state = 'indexing'
            mapping.update({'state': state, 'progress': status.get('progress', 0)})
            await self._update(
                file,
                datasets=list(mappings.values()),
                state='indexing',
                progress=min(90, 10 + int(70 * (offset + 1) / max(1, len(dataset_ids)))),
                message='RAGFlow разбирает и индексирует документ',
            )

        started = time.monotonic()
        while time.monotonic() - started < HERUVIM_RAGFLOW_SYNC_TIMEOUT_SECONDS:
            all_terminal = True
            has_failure = False
            progress_values = []
            for dataset_id, mapping in mappings.items():
                status = await self._document_status(dataset_id, mapping['document_id'])
                state = RAGFLOW_RUN_STATES.get(str(status.get('run', '')).upper(), 'indexing')
                progress = float(status.get('progress') or 0)
                mapping.update(
                    {
                        'state': state,
                        'progress': progress,
                        'chunk_count': status.get('chunk_count', status.get('chunk_num', 0)),
                        'token_count': status.get('token_count', status.get('token_num', 0)),
                        'message': status.get('progress_msg', ''),
                    }
                )
                progress_values.append(progress)
                all_terminal = all_terminal and state in TERMINAL_STATES
                has_failure = has_failure or state in {'failed', 'cancelled'}
            overall = int(10 + 90 * (sum(progress_values) / max(1, len(progress_values))))
            await self._update(
                file,
                datasets=list(mappings.values()),
                state='failed' if all_terminal and has_failure else ('ready' if all_terminal else 'indexing'),
                progress=min(100, overall),
                message=(
                    'Индексация завершена'
                    if all_terminal and not has_failure
                    else 'RAGFlow разбирает и индексирует документ'
                ),
                completed_at=int(time.time()) if all_terminal else None,
            )
            if all_terminal:
                return
            await asyncio.sleep(HERUVIM_RAGFLOW_SYNC_POLL_SECONDS)
        raise TimeoutError('RAGFlow не завершил индексацию за отведённое время')

    async def _update(self, file: FileModel, **changes: Any) -> None:
        latest = await Files.get_file_by_id(file.id) or file
        sync = dict((latest.data or {}).get('ragflow_sync') or {})
        sync.update(changes)
        sync['updated_at'] = int(time.time())
        await Files.update_file_data_by_id(file.id, {'ragflow_sync': sync})


ingestion_queue = RAGFlowIngestionQueue()
