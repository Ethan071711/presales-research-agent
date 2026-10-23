import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .auth import Actor


class State(TypedDict, total=False):
    question: str
    actor: Actor
    queries: list[str]
    findings: list
    gaps: list[str]
    round: int
    errors: list[str]
    report: str
    review_reasons: list[str]


class Research:
    def __init__(self, store, metrics, model=None, max_rounds=2, min_score=.55):
        if not 1 <= max_rounds <= 3:
            raise ValueError('max_rounds must be between 1 and 3')
        self.store, self.metrics, self.model = store, metrics, model
        self.max_rounds, self.min_score = max_rounds, min_score

    def run(self, question, actor, emit=lambda stage, data: None):
        if not isinstance(question, str) or not 2 <= len(question.strip()) <= 1000:
            raise ValueError('需求长度必须为2到1000个字符')
        start = time.perf_counter()

        def fallback(cause):
            self.metrics.fallbacks.labels(cause).inc()
            emit('fallback', {'cause': cause})

        def decompose(state):
            errors = []
            queries = [q.strip() for q in re.split('[；;？?\n]', state['question']) if q.strip()][:3]
            if self.model:
                try:
                    queries = self.model.plan(state['question'])
                except Exception as error:
                    errors.append('模型计划失败，已改用原问题分句：'+type(error).__name__)
                    fallback('planner')
            if not queries:
                queries = [state['question']]
            emit('decompose', {'queries': queries, 'mode': 'local-model' if self.model and not errors else 'deterministic'})
            return {'queries': queries, 'round': 0, 'findings': [], 'gaps': [], 'errors': errors}

        def search(state):
            results, gaps, errors = {}, [], list(state['errors'])

            def lookup(query):
                self.metrics.retrievals.inc()
                return query, self.store.search(query, actor, k=5)

            # Independent searches are dispatched together. PGlite serializes SQL internally;
            # normal PostgreSQL can execute the same searches on separate connections.
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(lookup, q) for q in state['queries'][:3]]
                for future in futures:
                    try:
                        query, hits = future.result()
                        usable = [h for h in hits if h.score >= self.min_score]
                        if not usable:
                            gaps.append(query)
                        for hit in usable:
                            results[hit.id] = hit
                    except Exception as error:
                        errors.append('检索失败：'+type(error).__name__)
                        gaps.append('资料检索服务不可用')
                        fallback('retrieval')
            for hit in state['findings']:
                results[hit.id] = hit
            findings = sorted(results.values(), key=lambda hit: hit.score, reverse=True)[:12]
            emit('parallel_search', {'round': state['round']+1, 'queries': state['queries'],
                                     'hits': [h.as_dict() for h in findings]})
            return {'findings': findings, 'gaps': gaps, 'errors': errors, 'round': state['round']+1}

        def assess(state):
            emit('assess', {'evidence_count': len(state['findings']), 'gaps': state['gaps'],
                            'round': state['round'], 'max_rounds': self.max_rounds})
            return {}

        def route(state):
            return 'refine' if state['gaps'] and state['round'] < self.max_rounds else 'synthesize'

        def refine(state):
            # Bounded query expansion, preserving the original request rather than inventing facts.
            queries = [(q[:250]+' 产品规格 兼容要求 交付政策') for q in state['gaps'][:3]]
            emit('refine', {'queries': queries})
            return {'queries': queries}

        def synthesize(state):
            findings = state['findings']
            reasons = ['方案为研究草稿，发布及正式报价须人工确认']
            if state['gaps']:
                reasons.append('部分需求缺乏足够检索证据')
            if state['errors']:
                reasons.append('运行触发过降级，需要检查过程记录')
            report = '\n\n'.join(f'- {h.title}：{h.text} [{h.id}]' for h in findings)
            mode = 'extractive'
            if not report:
                report = '未找到权限范围内足以支持当前需求的资料，请补充需求或交由人工处理。'
            elif self.model:
                try:
                    report = self.model.synthesize(question, findings)
                    mode = self.model.mode
                except Exception as error:
                    fallback('synthesis')
                    reasons.append('生成结果未通过引用检查，已降级为证据摘录')
            emit('synthesize', {'mode': mode, 'report': report, 'review_reasons': reasons})
            return {'report': report, 'review_reasons': reasons}

        graph = StateGraph(State)
        for name, node in [('decompose', decompose), ('search', search), ('assess', assess),
                           ('refine', refine), ('synthesize', synthesize)]:
            graph.add_node(name, node)
        graph.add_edge(START, 'decompose')
        graph.add_edge('decompose', 'search')
        graph.add_edge('search', 'assess')
        graph.add_conditional_edges('assess', route, {'refine': 'refine', 'synthesize': 'synthesize'})
        graph.add_edge('refine', 'search')
        graph.add_edge('synthesize', END)
        state = graph.compile().invoke({'question': question.strip(), 'actor': actor},
                                       config={'recursion_limit': 16})
        self.metrics.runs.labels('needs_review').inc()
        return {'report': state['report'], 'evidence': [h.as_dict() for h in state['findings']],
                'gaps': state['gaps'], 'rounds': state['round'], 'errors': state['errors'],
                'review_reasons': state['review_reasons'], 'status': 'needs_review',
                'duration_seconds': round(time.perf_counter()-start, 3),
                'data_provenance': 'new-synthetic-2026-10-24', 'paid_api_calls': 0}
