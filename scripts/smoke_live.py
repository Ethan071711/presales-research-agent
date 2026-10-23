"""Run after starting run.py. Exercise actual HTTP, SSE and official MCP client."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE = 'http://127.0.0.1:8128'
ROOT = Path(__file__).resolve().parents[1]


def login(client, role='sales', tenant='demo-a'):
    response = client.post(BASE+'/demo/session', json={'role': role, 'tenant': tenant})
    response.raise_for_status()
    return {'Authorization': 'Bearer '+response.json()['access_token']}


async def mcp_smoke(headers):
    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        async with streamable_http_client(BASE+'/tools/mcp', http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [tool.name for tool in tools.tools]
                assert set(names) == {'search_knowledge', 'create_quote'}
                retrieval = await session.call_tool('search_knowledge', {'query': '星桥交换机基础版的价格和规格'})
                assert not retrieval.isError
                quoted = await session.call_tool('create_quote', {
                    'items': [{'product_id': 'DEMO-SW-B', 'quantity': 3}], 'discount': '0.95'})
                assert not quoted.isError
                text = '\n'.join(block.text for block in quoted.content if hasattr(block, 'text'))
                assert '2850.00' in text
                return {'tools': names, 'protocol_initialize': 'passed',
                        'knowledge_tool': 'passed', 'quote_tool_expected_yuan': '2850.00'}


def main():
    with httpx.Client(timeout=180) as client:
        health = client.get(BASE+'/health').json()
        h = login(client)
        question = '星桥交换机基础版的价格和规格是什么？基础交换机搭配无线接入点需要注意什么？报价有效期多久？'
        started = time.perf_counter()
        response = client.post(BASE+'/research', headers=h, json={'question': question})
        response.raise_for_status()
        rid = response.json()['run_id']
        events = []
        with client.stream('GET', BASE+'/runs/'+rid+'/events', headers=h) as stream:
            stream.raise_for_status()
            for line in stream.iter_lines():
                if line.startswith('data: '):
                    events.append(json.loads(line[6:]))
        run = client.get(BASE+'/runs/'+rid, headers=h).json()
        assert run['status'] == 'needs_review', run
        assert events[-1]['stage'] == 'finished'
        assert run['result']['evidence']
        assert client.get(BASE+'/runs/'+rid, headers=login(client, tenant='demo-b')).status_code == 404
        body = {'decision': 'approved', 'note': '已核对本次模拟证据及引用。'}
        assert client.post(BASE+'/runs/'+rid+'/review', headers=h, json=body).status_code == 403
        manager = login(client, role='manager')
        approved = client.post(BASE+'/runs/'+rid+'/review', headers=manager, json=body)
        approved.raise_for_status()
        assert approved.json()['status'] == 'approved'
        metrics = client.get(BASE+'/metrics', headers=manager).text
        mcp_result = asyncio.run(mcp_smoke(h))
        result = {'measured_at_utc': datetime.now(timezone.utc).isoformat(), 'health': health,
                  'run_id': rid, 'duration_seconds': round(time.perf_counter()-started, 3),
                  'sse_stages': [e['stage'] for e in events], 'research_result': run['result'],
                  'cross_tenant_run_denied': True, 'sales_self_approval_denied': True,
                  'manager_approval': 'passed', 'mcp': mcp_result,
                  'prometheus_sample': metrics.splitlines(), 'paid_api_calls': 0}
        (ROOT/'results').mkdir(exist_ok=True)
        path = ROOT/'results/live_smoke_20261024.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'health': health, 'sse_stages': result['sse_stages'], 'mcp': mcp_result,
                          'duration_seconds': result['duration_seconds'],
                          'report': run['result']['report'], 'errors': run['result']['errors'],
                          'review_reasons': run['result']['review_reasons']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
