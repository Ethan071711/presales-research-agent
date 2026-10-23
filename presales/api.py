import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, issue_token, local_signing_key, verify_token
from .observability import Metrics, RunStore
from .quotation import CATALOG, quote
from .research import Research

ROOT = Path(__file__).resolve().parents[1]


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=2, max_length=1000)


class Line(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: str = Field(min_length=1, max_length=60)
    quantity: int = Field(strict=True, ge=1, le=100)


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[Line] = Field(min_length=1, max_length=20)
    discount: str = Field(default='1.00', pattern=r'^(0\.[0-9]{1,2}|1(\.0{1,2})?)$')
    budget: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Review(BaseModel):
    decision: Literal['approved', 'rejected']
    note: str = Field(min_length=2, max_length=500)


class DemoIdentity(BaseModel):
    tenant: Literal['demo-a', 'demo-b'] = 'demo-a'
    role: Literal['viewer', 'sales', 'manager'] = 'sales'


def create_app(store, storage_dir: Path, model=None, demo_mode=True, metrics=None):
    metrics = metrics or Metrics()
    key = local_signing_key(storage_dir)
    runs = RunStore(storage_dir/'runs.sqlite3')
    runs.recover_interrupted()
    worker = Research(store, metrics, model=model)
    pool = ThreadPoolExecutor(max_workers=4)
    bearer = HTTPBearer(auto_error=False)
    resource_url = 'http://127.0.0.1:8128/tools/mcp'

    class JWTVerifier:
        async def verify_token(self, token):
            try:
                actor = verify_token(token, key)
                return AccessToken(token=token, client_id=actor.subject, scopes=['presales:tools'],
                                   subject=actor.subject, resource=resource_url,
                                   claims={'tenant': actor.tenant, 'role': actor.role})
            except (jwt.InvalidTokenError, TypeError, ValueError):
                metrics.denied.labels('mcp-auth').inc()
                return None

    mcp = FastMCP('Presales research demo', stateless_http=True, json_response=True,
                  token_verifier=JWTVerifier(), auth=AuthSettings(
                      issuer_url='http://127.0.0.1:8128/demo', resource_server_url=resource_url,
                      required_scopes=['presales:tools'], validate_token_resource=False))

    def tool_actor():
        token = get_access_token()
        if not token or not token.claims:
            raise PermissionError('Authenticated HTTP identity required')
        return Actor(token.subject, token.claims['tenant'], token.claims['role'])

    @mcp.tool()
    def search_knowledge(query: Annotated[str, Field(min_length=2, max_length=1000)],
                         k: Annotated[int, Field(ge=1, le=10)] = 5) -> dict:
        """Search synthetic documents. Tenant/role come only from the verified bearer token."""
        metrics.retrievals.inc()
        return {'hits': [h.as_dict() for h in store.search(query, tool_actor(), k)]}

    @mcp.tool()
    def create_quote(items: list[Line], discount: str = '1.00', budget: float | None = None) -> dict:
        """Calculate a DRAFT from catalog prices; discounts/compatibility can require review."""
        return quote([x.model_dump() for x in items], tool_actor(), discount, budget)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield
        pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title='Presales research reconstruction', version='0.1.0', lifespan=lifespan)
    app.state.runs, app.state.metrics, app.state.key, app.state.mcp = runs, metrics, key, mcp

    def identity(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        try:
            if not credentials:
                raise jwt.InvalidTokenError('missing token')
            return verify_token(credentials.credentials, key)
        except (jwt.InvalidTokenError, TypeError, ValueError):
            metrics.denied.labels('http-auth').inc()
            raise HTTPException(401, '请先获取有效的演示身份')

    def visible_run(run_id, actor):
        value = runs.get(run_id, actor)
        if value is None:
            metrics.denied.labels('run-acl').inc()
            raise HTTPException(404, '任务不存在或当前身份无权访问')
        return value

    @app.get('/')
    def home():
        return FileResponse(ROOT/'web/index.html')

    @app.get('/health')
    def health():
        return {'status': 'ok', 'backend': store.backend, 'demo_mode': demo_mode,
                'model': model.mode if model else 'extractive', 'paid_api_calls': 0}

    @app.post('/demo/session')
    def demo_session(value: DemoIdentity, request: Request):
        if not demo_mode or (request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}):
            raise HTTPException(403, '演示身份入口只允许本机使用')
        actor = Actor('demo-'+value.role, value.tenant, value.role)
        return {'access_token': issue_token(actor, key), 'expires_in': 1800,
                'identity': vars(actor), 'notice': '本机模拟身份，不是企业登录服务'}

    @app.get('/catalog')
    def catalog(actor: Actor = Depends(identity)):
        return [p for p in CATALOG if actor.can_read(p)]

    @app.post('/search')
    def search(value: Question, actor: Actor = Depends(identity)):
        metrics.retrievals.inc()
        return {'hits': [h.as_dict() for h in store.search(value.question, actor)]}

    @app.post('/quotes')
    def quotation(value: QuoteRequest, actor: Actor = Depends(identity)):
        try:
            return quote([x.model_dump() for x in value.items], actor, value.discount, value.budget)
        except PermissionError as error:
            metrics.denied.labels('quote-role').inc()
            raise HTTPException(403, str(error))
        except (ValueError, ArithmeticError) as error:
            raise HTTPException(422, str(error))

    def perform(run_id, question, actor):
        try:
            result = worker.run(question, actor, lambda stage, data: runs.event(run_id, stage, data))
            runs.event(run_id, 'finished', {'status': result['status']})
            runs.finish(run_id, result['status'], result)
        except Exception as error:
            metrics.runs.labels('failed').inc()
            runs.event(run_id, 'finished', {'status': 'failed', 'kind': type(error).__name__})
            runs.finish(run_id, 'failed', {'error': '运行未完成，请检查服务日志', 'kind': type(error).__name__})

    @app.post('/research', status_code=202)
    def research(value: Question, actor: Actor = Depends(identity)):
        run_id = runs.create(actor, value.question)
        pool.submit(perform, run_id, value.question, actor)
        return {'run_id': run_id, 'status': 'running'}

    @app.get('/runs/{run_id}')
    def get_run(run_id: str, actor: Actor = Depends(identity)):
        return visible_run(run_id, actor)

    @app.get('/runs/{run_id}/events')
    async def events(run_id: str, actor: Actor = Depends(identity)):
        visible_run(run_id, actor)

        async def stream():
            after = 0
            while True:
                for event in runs.events(run_id, after):
                    after = event['seq']
                    yield 'id: '+str(after)+'\ndata: '+json.dumps(event, ensure_ascii=False)+'\n\n'
                if runs.get(run_id, actor)['status'] != 'running':
                    # Status may change between the event query and this check. Drain events
                    # committed in that interval before closing the stream.
                    for event in runs.events(run_id, after):
                        after = event['seq']
                        yield 'id: '+str(after)+'\ndata: '+json.dumps(event, ensure_ascii=False)+'\n\n'
                    break
                yield ': keepalive\n\n'
                await asyncio.sleep(.25)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @app.post('/runs/{run_id}/review')
    def review(run_id: str, value: Review, actor: Actor = Depends(identity)):
        try:
            runs.review(run_id, actor, value.decision, value.note)
            return visible_run(run_id, actor)
        except PermissionError as error:
            metrics.denied.labels('review-role').inc()
            raise HTTPException(403, str(error))
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get('/metrics')
    def prometheus(actor: Actor = Depends(identity)):
        if actor.role != 'manager':
            raise HTTPException(403, '需要经理身份查看指标')
        return Response(metrics.render(), media_type='text/plain; version=0.0.4')

    app.mount('/tools', mcp.streamable_http_app())
    return app
