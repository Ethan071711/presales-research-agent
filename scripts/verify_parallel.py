"""Validate independent pgvector queries on actual PostgreSQL, with declared SQL latency."""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from presales.auth import Actor
from presales.retrieval import Embedder,PostgresStore,cached_vectors


class Probe(PostgresStore):
    probe=False
    def __init__(self,*args):
        self.intervals=[]
        self.interval_lock=threading.Lock()
        super().__init__(*args)

    @contextmanager
    def connect(self):
        with super().connect() as connection:
            if self.probe:
                start=time.perf_counter()
                pid=connection.execute('SELECT pg_backend_pid() AS pid,pg_sleep(0.3)').fetchone()['pid']
                end=time.perf_counter()
                with self.interval_lock:
                    self.intervals.append({'pid':pid,'start':start,'end':end})
            yield connection


def main():
    import torch
    torch.set_num_threads(1)
    url=os.environ['PRESALES_DATABASE_URL']
    embedder=Embedder()
    docs,vectors=cached_vectors(embedder,ROOT/'.storage')
    store=Probe(url,embedder)
    assert not store.serial_queries,'Use actual PostgreSQL; PGlite intentionally serializes SQL'
    store.load(docs,vectors)
    measurements={}
    for serial in [True,False]:
        store.serial_queries=serial;store.probe=True;store.intervals=[]
        start=time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results=list(pool.map(lambda q:store.search(q,Actor('parallel-test','demo-a','sales')),['Fox EP11 单相','已有光伏改造','停电备电需求']))
        elapsed=time.perf_counter()-start
        measurements['serialized' if serial else 'parallel']={'seconds':round(elapsed,3),'intervals':store.intervals,'nonempty_queries':sum(bool(r) for r in results)}
    intervals=measurements['parallel']['intervals']
    overlap=max(x['start'] for x in intervals)<min(x['end'] for x in intervals)
    assert len({x['pid'] for x in intervals})==3 and overlap
    assert measurements['parallel']['seconds']<measurements['serialized']['seconds']*.8
    result={'actual_database':'PostgreSQL/pgvector','injected_sql_delay_seconds':.3,'queries':3,
            'distinct_connections':3,'sql_execution_overlapped':overlap,'measurements':measurements,
            'note':'Injected pg_sleep validates concurrency; these timings are not production query latency.'}
    (ROOT/'results/parallel_postgres_20261024.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
