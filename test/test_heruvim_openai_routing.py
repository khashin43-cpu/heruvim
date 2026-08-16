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
