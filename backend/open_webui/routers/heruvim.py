"""ХЕРУВИМ-specific ingestion, RAGFlow document status and preview API."""

import re

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from open_webui.config import (
    HERUVIM_RAGFLOW_API_KEY,
    HERUVIM_RAGFLOW_BASE_URL,
    HERUVIM_RAGFLOW_CHAT_ID,
    HERUVIM_RAGFLOW_DATASET_IDS,
    HERUVIM_RAGFLOW_ENABLED,
)
from open_webui.internal.heruvim_ragflow_ingestion import ingestion_queue, public_ingestion_record
from open_webui.models.files import Files
from open_webui.utils.auth import get_verified_user

router = APIRouter()


async def _public_base_url(request: Request) -> str:
    from open_webui.models.config import Config

    configured_url = str(await Config.get('webui.url') or '').strip().rstrip('/')
    if configured_url:
        parsed = httpx.URL(configured_url)
        if parsed.host in {'localhost', '127.0.0.1'} and parsed.port == 5173:
            return str(parsed.copy_with(port=8080)).rstrip('/')
        return configured_url
    base_url = str(request.base_url).rstrip('/')
    parsed = httpx.URL(base_url)
    if parsed.host in {'localhost', '127.0.0.1'} and parsed.port == 5173:
        return str(parsed.copy_with(port=8080)).rstrip('/')
    return base_url


def _document_url(document_id: str, public_base_url: str, *, download: bool = False) -> str:
    path = f'/api/v1/heruvim/ragflow/documents/{document_id}/preview'
    if download:
        path = f'{path}?download=1'
    return f'{public_base_url.rstrip("/")}{path}'


def _ragflow_headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {HERUVIM_RAGFLOW_API_KEY}'}


async def _ragflow_get(path: str, **kwargs) -> httpx.Response:
    if not HERUVIM_RAGFLOW_ENABLED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Интеграция RAGFlow не настроена')
    async with httpx.AsyncClient(base_url=HERUVIM_RAGFLOW_BASE_URL, headers=_ragflow_headers(), timeout=60) as client:
        return await client.get(path, **kwargs)


async def _ragflow_delete(path: str, **kwargs) -> httpx.Response:
    if not HERUVIM_RAGFLOW_ENABLED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='База знаний не настроена')
    try:
        async with httpx.AsyncClient(
            base_url=HERUVIM_RAGFLOW_BASE_URL,
            headers=_ragflow_headers(),
            timeout=60,
        ) as client:
            return await client.request('DELETE', path, **kwargs)
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='RAGFlow временно недоступен. Попробуйте удалить документ ещё раз',
        ) from exc


async def _ragflow_dataset_ids() -> list[str]:
    if HERUVIM_RAGFLOW_DATASET_IDS:
        return list(dict.fromkeys(HERUVIM_RAGFLOW_DATASET_IDS))

    response = await _ragflow_get(f'/api/v1/chats/{HERUVIM_RAGFLOW_CHAT_ID}')
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail='Не удалось получить datasets RAGFlow chat assistant',
        )
    payload = response.json()
    chat = payload.get('data') or {}
    dataset_ids = chat.get('dataset_ids') or chat.get('kb_ids') or []
    return [str(value) for value in dataset_ids if value]


def _filename_from_disposition(value: str) -> str:
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", value or '', re.I)
    return match.group(1).strip() if match else ''


async def _user_document_mappings(user) -> tuple[list, dict[str, list[tuple[str, str]]]]:
    files = await (Files.get_files() if user.role == 'admin' else Files.get_files_by_user_id(user.id))
    mappings: dict[str, list[tuple[str, str]]] = {}
    for file in files:
        sync = dict((file.data or {}).get('ragflow_sync') or {})
        for dataset in sync.get('datasets') or []:
            if not isinstance(dataset, dict):
                continue
            document_id = str(dataset.get('document_id') or '').strip()
            dataset_id = str(dataset.get('dataset_id') or '').strip()
            if document_id and dataset_id:
                mappings.setdefault(document_id, []).append((file.id, dataset_id))
    return files, mappings


@router.get('/ingestion')
async def list_ingestion_jobs(user=Depends(get_verified_user)):
    files = await (Files.get_files() if user.role == 'admin' else Files.get_files_by_user_id(user.id))
    jobs = [public_ingestion_record(file) for file in files if (file.data or {}).get('ragflow_sync')]
    jobs.sort(key=lambda item: item.get('updated_at') or 0, reverse=True)
    return {
        'enabled': ingestion_queue.enabled,
        'configured': HERUVIM_RAGFLOW_ENABLED,
        'sync_attachments': ingestion_queue.sync_attachments,
        'chat_id': HERUVIM_RAGFLOW_CHAT_ID if user.role == 'admin' else None,
        'queue_size': ingestion_queue.size,
        'jobs': jobs,
    }


@router.post('/ingestion/{file_id}/retry')
async def retry_ingestion(file_id: str, user=Depends(get_verified_user)):
    file = await Files.get_file_by_id(file_id)
    if not file or (user.role != 'admin' and file.user_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Файл не найден')
    if not ingestion_queue.enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Интеграция RAGFlow не настроена')
    queued = await ingestion_queue.enqueue(file.id, file.user_id, force=True)
    return {'status': True, 'queued': queued, 'file_id': file.id}


@router.get('/ragflow/documents')
async def list_ragflow_documents(request: Request, user=Depends(get_verified_user)):
    dataset_ids = await _ragflow_dataset_ids()
    public_base_url = await _public_base_url(request)
    _, user_document_mappings = await _user_document_mappings(user)
    datasets = []
    for dataset_id in dataset_ids:
        docs = []
        total = None
        page = 1
        error = None
        while page <= 100:
            response = await _ragflow_get(
                f'/api/v1/datasets/{dataset_id}/documents',
                params={'page': page, 'page_size': 100},
            )
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if response.status_code >= 400 or (isinstance(payload, dict) and payload.get('code') not in (None, 0)):
                error = {
                    'status_code': response.status_code,
                    'message': payload.get('message') if isinstance(payload, dict) else None,
                }
                break

            data = payload.get('data') or {}
            page_docs = data.get('docs') or data.get('documents') or data.get('items') or []
            docs.extend(page_docs)
            try:
                total = int(data.get('total'))
            except (TypeError, ValueError):
                total = None

            if not page_docs or (total is not None and len(docs) >= total) or len(page_docs) < 100:
                break
            page += 1

        if error:
            datasets.append({'dataset_id': dataset_id, 'ok': False, **error})
            continue

        docs = list({str(doc.get('id')): doc for doc in docs if doc.get('id')}.values())
        datasets.append(
            {
                'dataset_id': dataset_id,
                'ok': True,
                'total': total if total is not None else len(docs),
                'documents': [
                    {
                        'id': doc.get('id'),
                        'name': doc.get('name'),
                        'run': doc.get('run'),
                        'status': doc.get('status'),
                        'type': doc.get('type'),
                        'size': doc.get('size'),
                        'chunk_num': doc.get('chunk_num'),
                        'created_at': doc.get('create_time') or doc.get('created_at'),
                        'updated_at': doc.get('update_time') or doc.get('updated_at'),
                        'preview_url': _document_url(str(doc.get('id')), public_base_url),
                        'download_url': _document_url(str(doc.get('id')), public_base_url, download=True),
                        'can_delete': user.role == 'admin' or str(doc.get('id')) in user_document_mappings,
                    }
                    for doc in docs
                ],
            }
        )

    return {
        'configured': HERUVIM_RAGFLOW_ENABLED,
        'chat_id': HERUVIM_RAGFLOW_CHAT_ID if user.role == 'admin' else None,
        'dataset_ids': dataset_ids,
        'datasets': datasets,
    }


@router.delete('/ragflow/datasets/{dataset_id}/documents/{document_id}')
async def delete_ragflow_document(dataset_id: str, document_id: str, user=Depends(get_verified_user)):
    dataset_ids = await _ragflow_dataset_ids()
    if dataset_id not in dataset_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Раздел базы знаний не найден')

    files, user_document_mappings = await _user_document_mappings(user)
    if user.role != 'admin' and not any(
        mapped_dataset_id == dataset_id for _, mapped_dataset_id in user_document_mappings.get(document_id, [])
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Можно удалять только добавленные Вами документы',
        )

    response = await _ragflow_delete(
        f'/api/v1/datasets/{dataset_id}/documents',
        json={'ids': [document_id], 'delete_all': False},
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get('code') not in (None, 0)):
        detail = payload.get('message') if isinstance(payload, dict) else None
        raise HTTPException(
            status_code=response.status_code if response.status_code >= 400 else status.HTTP_502_BAD_GATEWAY,
            detail=detail or 'Не удалось удалить документ из базы знаний',
        )

    for file in files:
        data = dict(file.data or {})
        sync = dict(data.get('ragflow_sync') or {})
        datasets = [
            item
            for item in sync.get('datasets') or []
            if not (
                isinstance(item, dict)
                and str(item.get('dataset_id') or '') == dataset_id
                and str(item.get('document_id') or '') == document_id
            )
        ]
        if len(datasets) == len(sync.get('datasets') or []):
            continue
        sync['datasets'] = datasets
        sync['state'] = 'ready' if datasets else 'cancelled'
        sync['progress'] = 100 if datasets else 0
        sync['message'] = 'Документ удалён из базы знаний' if not datasets else 'Удалён из одного раздела базы знаний'
        sync['error'] = None
        await Files.update_file_data_by_id(file.id, {'ragflow_sync': sync})

    return {'ok': True, 'document_id': document_id, 'dataset_id': dataset_id}


@router.get('/ragflow/documents/{document_id}/preview')
async def preview_ragflow_document(
    document_id: str,
    download: bool = Query(False),
    user=Depends(get_verified_user),
):
    response = await _ragflow_get(f'/api/v1/documents/{document_id}/preview')
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail='Не удалось получить документ из RAGFlow')

    content_type = response.headers.get('content-type') or 'application/octet-stream'
    content_disposition = response.headers.get('content-disposition') or ''
    filename = _filename_from_disposition(content_disposition)
    headers = {}
    if filename:
        disposition = 'attachment' if download else 'inline'
        headers['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    return Response(content=response.content, media_type=content_type, headers=headers)
