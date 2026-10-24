import json
import sqlite3
import time
import uuid
from decimal import Decimal
from pathlib import Path

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter('presales_http_requests_total','HTTP requests, denominator for rejected-request rate',registry=self.registry)
        self.attempts = Counter('presales_stage_attempts_total','Stage attempts, denominator for fallback rate',['cause'],registry=self.registry)
        self.runs = Counter('presales_runs_total', 'Research runs by final status', ['status'], registry=self.registry)
        self.fallbacks = Counter('presales_fallbacks_total', 'Fallbacks by cause', ['cause'], registry=self.registry)
        self.denied = Counter('presales_denied_total', 'Denied requests', ['kind'], registry=self.registry)
        self.retrievals = Counter('presales_retrievals_total', 'Authorized retrieval requests', registry=self.registry)
        self.retrieval_candidates=Counter('presales_retrieval_candidates_total','Records audited after pre-filtered retrieval',registry=self.registry)
        self.retrieval_unauthorized=Counter('presales_retrieval_unauthorized_total','Records blocked by independent retrieval permission audit',registry=self.registry)
        self.llm_time = Histogram('presales_llm_seconds', 'Actual model generation latency', ['mode'], registry=self.registry)
        self.llm_tokens = Counter('presales_llm_tokens_total', 'Measured model tokens', ['direction'], registry=self.registry)
        self.cost = Counter('presales_llm_cost_cny_total', 'Token cost estimated from configured CNY rates; not a provider invoice', registry=self.registry)

    def usage(self,mode,seconds,input_tokens,output_tokens,input_rate='0',output_rate='0'):
        rates=[Decimal(str(input_rate)),Decimal(str(output_rate))]
        if any(not r.is_finite() or r<0 for r in rates):
            raise ValueError('Model prices must be non-negative finite CNY amounts per million tokens')
        if any(type(v) is not int or v<0 for v in [input_tokens,output_tokens]):
            raise ValueError('Token usage must be non-negative integers')
        amount=(rates[0]*input_tokens+rates[1]*output_tokens)/Decimal(1000000)
        self.llm_time.labels(mode).observe(seconds)
        self.llm_tokens.labels('input').inc(input_tokens)
        self.llm_tokens.labels('output').inc(output_tokens)
        self.cost.inc(float(amount))
        return str(amount)

    def render(self):
        return generate_latest(self.registry)


class RunStore:
    """Durable trace and review decisions; not a claim of production distributed tracing."""
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, tenant TEXT NOT NULL,
                status TEXT NOT NULL, question TEXT NOT NULL, result TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                stage TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);''')

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def create(self, actor, question):
        run_id = uuid.uuid4().hex
        with self.connect() as c:
            c.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',
                      (run_id, actor.subject, actor.tenant, 'running', question, None, time.time()))
        return run_id

    def event(self, run_id, stage, payload):
        with self.connect() as c:
            c.execute('INSERT INTO events(run_id,stage,payload,created) VALUES (?,?,?,?)',
                      (run_id, stage, json.dumps(payload, ensure_ascii=False), time.time()))

    def finish(self, run_id, status, result):
        with self.connect() as c:
            c.execute('UPDATE runs SET status=?,result=? WHERE id=?',
                      (status, json.dumps(result, ensure_ascii=False), run_id))

    def get(self, run_id, actor):
        with self.connect() as c:
            row = c.execute('SELECT * FROM runs WHERE id=? AND tenant=? AND (subject=? OR ?=?)',
                            (run_id, actor.tenant, actor.subject, actor.role, 'manager')).fetchone()
        if not row:
            return None
        data = dict(row)
        data['result'] = json.loads(data['result']) if data['result'] else None
        return data

    def events(self, run_id, after=0):
        with self.connect() as c:
            rows = c.execute('SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq', (run_id, after)).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in rows]

    def review(self, run_id, actor, decision, note):
        if actor.role != 'manager':
            raise PermissionError('需要同租户经理审核')
        with self.connect() as c:
            cursor = c.execute("UPDATE runs SET status=? WHERE id=? AND tenant=? AND status='needs_review'",
                               (decision, run_id, actor.tenant))
            if cursor.rowcount != 1:
                raise ValueError('没有可供当前身份审核的任务')
        self.event(run_id, 'human_review', {'decision': decision, 'reviewer': actor.subject, 'note': note})

    def recover_interrupted(self):
        # A process crash is visible after restart; do not silently label the run successful.
        with self.connect() as c:
            c.execute("UPDATE runs SET status='interrupted' WHERE status='running'")

    def claim_resume(self,run_id,actor):
        with self.connect() as c:
            changed=c.execute("UPDATE runs SET status='running' WHERE id=? AND tenant=? AND subject=? AND status='interrupted'",
                              (run_id,actor.tenant,actor.subject))
            return changed.rowcount==1
