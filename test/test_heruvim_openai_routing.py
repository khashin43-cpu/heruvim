import json

import pytest

from open_webui.routers import openai


@pytest.mark.parametrize(
    'query',
    [
        'Как работает Херувим?',
        'Опиши MCP, которые тебе доступны',
        'Опиши mсp которые тебе доступны',
        'Какие инструменты подключены?',
        'Что ты умеешь?',
    ],
)
def test_capability_queries_do_not_require_ragflow(query):
    assert openai._is_heruvim_capability_query(query)


def test_explicit_indexed_corpus_request_can_use_ragflow():
    assert not openai._is_heruvim_capability_query(
        'Найди в базе знаний документацию про доступные MCP'
    )


def test_capability_planning_removes_ragflow_tool():
    payload = {
        'tools': [
            openai._HERUVIM_RAGFLOW_TOOL_SCHEMA,
            {
                'type': 'function',
                'function': {'name': 'connected_mcp_tool', 'parameters': {'type': 'object'}},
            },
        ]
    }

    openai._add_heruvim_ragflow_tool(payload, include_ragflow=False)

    names = [(tool.get('function') or {}).get('name') for tool in payload['tools']]
    assert openai._HERUVIM_RAGFLOW_TOOL_NAME not in names
    assert 'connected_mcp_tool' in names
    assert openai._HERUVIM_READ_DOCUMENT_TOOL_NAME in names


def test_current_docx_attachment_uses_docx_reader_tool():
    payload = {
        'messages': [
            {
                'role': 'system',
                'content': (
                    'HERUVIM_CURRENT_CHAT_ATTACHMENTS:\n'
                    'file_id="file-1" name="report.docx" '
                    'local_path="/tmp/report.docx" preferred_tool="heruvim_docx_read"'
                ),
            },
            {'role': 'user', 'content': 'Изучите приложенный документ'},
        ]
    }

    tool_call = openai._synthesize_current_attachment_tool_call(payload)

    assert tool_call['function']['name'] == 'heruvim_docx_read'
    arguments = json.loads(tool_call['function']['arguments'])
    assert arguments['path'] == '/tmp/report.docx'
    assert arguments['_heruvim_file_id'] == 'file-1'


def test_attachment_result_gets_open_and_download_links():
    tool_call = {
        'function': {
            'name': 'heruvim_docx_read',
            'arguments': json.dumps(
                {
                    'path': '/tmp/report.docx',
                    '_heruvim_file_id': 'file-1',
                    '_heruvim_file_name': 'report.docx',
                }
            ),
        }
    }

    result = openai._add_current_attachment_links({'ok': True}, tool_call, 'http://localhost:8080')

    assert result['preview_url'] == 'http://localhost:8080/api/v1/files/file-1/content'
    assert result['download_url'].endswith('/api/v1/files/file-1/content?attachment=true')


def test_thinking_defaults_are_explicit(monkeypatch):
    monkeypatch.setattr(openai, 'HERUVIM_LLM_ENABLE_THINKING', True)
    monkeypatch.setattr(openai, 'HERUVIM_LLM_THINKING_BUDGET', 512)
    payload = {}

    openai._apply_heruvim_llm_defaults(payload)

    assert payload == {'enable_thinking': True, 'thinking_budget': 512}
