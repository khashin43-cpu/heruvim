#!/usr/bin/env python3
"""Prepare or apply the ХЕРУВИМ profile to an existing RAGFlow chat assistant."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / 'heruvim' / 'system_prompt_ru.md'


def build_payload() -> dict:
    prompt = PROMPT_PATH.read_text(encoding='utf-8')
    return {
        'name': 'ХЕРУВИМ',
        'llm_setting': {
            'temperature': 0.1,
            'top_p': 0.3,
            'presence_penalty': 0.2,
            'frequency_penalty': 0.2,
        },
        'prompt_config': {
            'system': prompt,
            'prologue': (
                'Я готов. Сформулируйте задачу или приложите документ.'
            ),
            'parameters': [{'key': 'knowledge', 'optional': True}],
            'empty_response': '',
            'quote': True,
            'refine_multiturn': True,
            'reasoning': False,
        },
    }


def patch_assistant(base_url: str, api_key: str, chat_id: str, payload: dict) -> dict:
    url = f"{base_url.rstrip('/')}/api/v1/chats/{chat_id.strip('/')}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'RAGFlow returned HTTP {exc.code}: {detail}') from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='apply the PATCH request; otherwise print a dry run')
    args = parser.parse_args()

    base_url = os.getenv('HERUVIM_RAGFLOW_BASE_URL', 'http://127.0.0.1:9380')
    api_key = os.getenv('HERUVIM_RAGFLOW_API_KEY', '').strip()
    chat_id = os.getenv('HERUVIM_RAGFLOW_CHAT_ID', '').strip()
    payload = build_payload()

    if not args.apply:
        preview = {
            'url': f"{base_url.rstrip('/')}/api/v1/chats/<CHAT_ID>",
            'payload': payload,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print('\nDry run only. Set HERUVIM_RAGFLOW_API_KEY and HERUVIM_RAGFLOW_CHAT_ID, then add --apply.')
        return 0

    if not api_key or not chat_id:
        print('HERUVIM_RAGFLOW_API_KEY and HERUVIM_RAGFLOW_CHAT_ID are required.', file=sys.stderr)
        return 2

    result = patch_assistant(base_url, api_key, chat_id, payload)
    if result.get('code') not in (None, 0):
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    summary = {
        'status': 'updated',
        'chat_id': chat_id,
        'name': result.get('data', {}).get('name'),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
