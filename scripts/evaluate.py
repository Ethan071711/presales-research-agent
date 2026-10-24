"""Measured reconstruction results. No target score and no historical-company claims."""
import json
import os
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from presales.auth import Actor
from presales.retrieval import Embedder, MemoryStore, PostgresStore, cached_vectors
from presales.hybrid import HybridStore


def main():
    url = os.getenv('PRESALES_DATABASE_URL')
    if not url:
        raise SystemExit('Set PRESALES_DATABASE_URL to a local/owned pgvector database first.')
    embedder = Embedder(os.getenv('PRESALES_EMBED_MODEL', 'BAAI/bge-small-zh-v1.5'))
    docs, vectors = cached_vectors(embedder, ROOT/'.storage')
    dense_store = PostgresStore(url, embedder)
    dense_store.load(docs, vectors)
    store = HybridStore(dense_store)
    cases = [json.loads(x) for name in ['cases.jsonl','acl_cases.jsonl']
             for x in (ROOT/'eval'/name).read_text(encoding='utf-8').splitlines()]
    details, hits, positives, violations = [], 0, 0, 0
    categories = Counter()
    start = time.perf_counter()
    for case in cases:
        actor = Actor('eval', case['tenant'], case['role'])
        then = time.perf_counter()
        result = store.search(case['question'], actor, 5)
        ids = {h.id for h in result}
        relevant = set(case['relevant_ids'])
        hit = bool(ids & relevant) if relevant else None
        if relevant:
            positives += 1
            hits += int(hit)
            categories[case['category']+'_total'] += 1
            categories[case['category']+'_hits'] += int(hit)
        # Check every returned document against the actual ACL, not just one forbidden ID.
        by_id = {d['id']: d for d in docs}
        leaked = [i for i in ids if not actor.can_read(by_id[i])]
        violations += bool(leaked)
        details.append({'id': case['id'], 'category': case['category'], 'question': case['question'],
                        'relevant_ids': list(relevant), 'returned_ids': [h.id for h in result],
                        'hit_at_5': hit, 'acl_violations': leaked,
                        'latency_seconds': round(time.perf_counter()-then, 4)})
    with dense_store.connect() as connection:
        version = connection.execute('SELECT version() AS value').fetchone()['value']
        indexes = connection.execute("SELECT indexname,indexdef FROM pg_indexes WHERE tablename LIKE 'presales_%' AND indexdef LIKE '%USING hnsw%'").fetchall()
    plan = dense_store.explain_index(Actor('eval','demo-a','sales'),'Fox KH EP11 单相 8kW 31.08kWh')
    def index_names(value):
        if isinstance(value,dict):
            return ({value['Index Name']} if 'Index Name' in value else set()).union(
                *(index_names(item) for item in value.values()))
        if isinstance(value,list):
            return set().union(*(index_names(item) for item in value))
        return set()
    used_hnsw=sorted(index_names(plan)&{row['indexname'] for row in indexes})
    summary = {'measured_at_utc': datetime.now(timezone.utc).isoformat(),
               'provenance': 'NEW battery questions against manually curated public facts; not historic internship results',
               'backend': store.backend, 'database': version, 'embedding_model': embedder.model_name,
               'reranker': store.reranker.model_name, 'fusion': 'RRF(k=60); ACL before both retrieval paths',
               'python': platform.python_version(), 'documents': len(docs), 'cases': len(cases),
               'answerable_retrieval_cases': positives, 'recall_at_5': hits/positives,
               'hits_at_5': hits, 'acl_attack_cases': sum(bool(c['forbidden_ids']) for c in cases),
               'acl_violating_queries_all_240': violations, 'categories': dict(categories),
               'elapsed_seconds': round(time.perf_counter()-start, 3), 'hnsw_indexes': indexes,
               'forced_index_explain_for_validation': plan, 'verified_hnsw_indexes': used_hnsw,
               'paid_api_calls': 0,
               'limitations': ['Template-based queries share a small public-fact corpus; not independent real-user data.',
                              'Recall@5 measures retrieval, not quotation accuracy or generated-answer factuality.',
                              'Small tables may use sequential scan by default; forced EXPLAIN only verifies the HNSW path.',
                              ('PGlite/WASM serializes database work.' if dense_store.serial_queries else 'Native PostgreSQL uses independent connections; this is not a production-load benchmark.')]}
    out = ROOT/'results'
    out.mkdir(exist_ok=True)
    (out/'battery_hybrid_retrieval_20261025.json').write_text(json.dumps({'summary':summary,'details':details},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k not in {'forced_index_explain_for_validation','hnsw_indexes'}}, ensure_ascii=False, indent=2))
    assert violations == 0, 'ACL violation found'
    assert used_hnsw, 'EXPLAIN did not use a declared HNSW index'


if __name__ == '__main__':
    main()
