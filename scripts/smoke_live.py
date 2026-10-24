"""Run after starting run.py. Exercise actual HTTP, SSE and official MCP client."""
import asyncio
import json
import time
from datetime import date, datetime, timezone
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
                assert set(names)=={'search_knowledge','create_quote','select_battery'}
                retrieval=await session.call_tool('search_knowledge',{'query':'Fox KH 三相 EP11 的资料冲突'})
                assert not retrieval.isError
                selected=await session.call_tool('select_battery',{'profile':{'phase':'three','min_capacity_kwh':30,'min_package_power_kw':12}})
                assert not selected.isError
                assert 'sigen-three-15-36' in '\n'.join(b.text for b in selected.content if hasattr(b,'text'))
                quoted=await session.call_tool('create_quote',{'items':[{'product_id':'fox-single-ep11-8-31.08','quantity':1}]})
                assert not quoted.isError
                text = '\n'.join(block.text for block in quoted.content if hasattr(block, 'text'))
                assert 'awaiting_price' in text and '"total_aud": null' in text
                public_quote=await session.call_tool('create_quote',{'items':[{'product_id':'hyxi-three-e200-10-21.2','quantity':1}]})
                assert not public_quote.isError
                public_text='\n'.join(b.text for b in public_quote.content if hasattr(b,'text'))
                expected='reference_for_review' if date.today()<date(2026,11,1) else 'awaiting_price'
                assert expected in public_text and '"total_aud": null' in public_text
                return {'tools': names, 'protocol_initialize': 'passed',
                        'knowledge_tool':'passed','selection_tool':'passed','missing_price_remains_null':True,
                        'public_quote_status':expected,'public_offer_not_final_total':True}


def main():
    with httpx.Client(timeout=180) as client:
        health = client.get(BASE+'/health').json()
        h = login(client)
        quoted=client.post(BASE+'/quotes',headers=h,json={'items':[{'product_id':'hyxi-three-e200-10-21.2','quantity':1}],'budget':4000})
        quoted.raise_for_status()
        public_quote=quoted.json()
        assert public_quote['total_aud'] is None and public_quote['budget_status']=='unverified'
        if date.today()<date(2026,11,1):
            assert public_quote['public_reference']['amount_aud']=='3157.00'
        question='Fox KH 三相 EP11 套餐的相制是否存在资料冲突？已有光伏加装电池需要核对什么？官网没有公开售价时能直接给出金额吗？'
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
        usage=[e['payload'] for e in events if e['stage']=='llm_call']
        if health['model']=='local-transformers':
            assert usage and all('estimated_cost_cny' in u for u in usage)
        assert client.get(BASE+'/runs/'+rid, headers=login(client, tenant='demo-b')).status_code == 404
        body={'decision':'approved','note':'自动联调测试：确认公开资料草稿流程，未形成供应商报价。'}
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
                  'public_quote':public_quote,
                  'prometheus_sample':metrics.splitlines(),'model_usage':usage,
                  'cost_scope':'Configured-rate token estimate; local test, not a vendor invoice','paid_api_calls':0}
        (ROOT/'results').mkdir(exist_ok=True)
        path=ROOT/'results'/f'battery_live_smoke_{datetime.now():%Y%m%d}.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'health': health, 'sse_stages': result['sse_stages'], 'mcp': mcp_result,
                          'duration_seconds': result['duration_seconds'],
                          'report': run['result']['report'], 'errors': run['result']['errors'],
                          'review_reasons': run['result']['review_reasons']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
