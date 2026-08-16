import asyncio
from types import SimpleNamespace

import httpx
from open_webui.internal.heruvim_ragflow_ingestion import (
    RAGFlowIngestionQueue,
    canonical_document_id,
    public_ingestion_record,
)
from open_webui.internal import heruvim_ragflow_ingestion
from open_webui.routers import files, heruvim


def test_canonical_document_id_uses_content_hash():
    digest = 'a' * 64
    file = SimpleNamespace(id='file-1', meta={'file_hash': digest}, hash=None)

    assert canonical_document_id(file) == f'heruvim:{digest}'


def test_public_record_keeps_cross_system_mapping():
    file = SimpleNamespace(
        id='file-1',
        filename='report.pdf',
        hash=None,
        meta={'file_hash': 'b' * 64},
        data={
            'ragflow_sync': {
                'state': 'ready',
                'progress': 100,
                'datasets': [{'dataset_id': 'dataset-1', 'document_id': 'document-1'}],
            }
        },
        updated_at=123,
    )

    record = public_ingestion_record(file)

    assert record['canonical_document_id'] == f"heruvim:{'b' * 64}"
    assert record['state'] == 'ready'
    assert record['datasets'][0]['document_id'] == 'document-1'


def test_ragflow_delete_sends_json_body_with_generic_request(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append(('init', kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, path, **kwargs):
            calls.append(('request', method, path, kwargs))
            return httpx.Response(200, json={'code': 0, 'data': {'deleted': 1}})

    monkeypatch.setattr(heruvim, 'HERUVIM_RAGFLOW_ENABLED', True)
    monkeypatch.setattr(heruvim.httpx, 'AsyncClient', FakeClient)

    response = asyncio.run(
        heruvim._ragflow_delete(
            '/api/v1/datasets/dataset-1/documents',
            json={'ids': ['document-1'], 'delete_all': False},
        )
    )

    assert response.status_code == 200
    assert calls[1] == (
        'request',
        'DELETE',
        '/api/v1/datasets/dataset-1/documents',
        {'json': {'ids': ['document-1'], 'delete_all': False}},
    )


def test_internal_file_processing_flag_is_independent_from_ragflow(monkeypatch):
    monkeypatch.setattr(files, 'HERUVIM_OPENWEBUI_INTERNAL_FILE_PROCESSING', False)
    assert files._heruvim_ragflow_only_uploads()

    monkeypatch.setattr(files, 'HERUVIM_OPENWEBUI_INTERNAL_FILE_PROCESSING', True)
    assert not files._heruvim_ragflow_only_uploads()


def test_ingestion_queue_availability_is_independent_from_attachment_policy(monkeypatch):
    queue = RAGFlowIngestionQueue()
    monkeypatch.setattr(heruvim_ragflow_ingestion, 'HERUVIM_RAGFLOW_ENABLED', True)
    monkeypatch.setattr(heruvim_ragflow_ingestion, 'HERUVIM_RAGFLOW_SYNC_ATTACHMENTS', False)

    assert queue.enabled
    assert not queue.sync_attachments


def test_explicit_knowledge_upload_syncs_when_chat_attachment_sync_is_disabled(monkeypatch):
    monkeypatch.setattr(files, 'HERUVIM_RAGFLOW_SYNC_ATTACHMENTS', False)

    assert not files._should_sync_upload_to_ragflow(None, None)
    assert files._should_sync_upload_to_ragflow({'knowledge_id': 'knowledge-1'}, None)
    assert files._should_sync_upload_to_ragflow(None, True)


def test_explicit_upload_can_disable_ragflow_sync(monkeypatch):
    monkeypatch.setattr(files, 'HERUVIM_RAGFLOW_SYNC_ATTACHMENTS', True)

    assert not files._should_sync_upload_to_ragflow({'knowledge_id': 'knowledge-1'}, False)
