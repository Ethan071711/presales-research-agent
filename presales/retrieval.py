"""pgvector ANN within authorized ACL partitions; no post-retrieval permission stripping."""
import hashlib
import json
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Hit:
    id: str
    title: str
    text: str
    score: float
    product_id: str | None
    sources: list = field(default_factory=list)
    review_flags: list = field(default_factory=list)
    tenant: str | None = None
    roles: list | None = None

    def as_dict(self):
        return {k:v for k,v in vars(self).items() if k not in {'tenant','roles'}}


class Embedder:
    def __init__(self, model='BAAI/bge-small-zh-v1.5', device='cpu'):
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model, device=device)
        self.model_name = model

    def documents(self, texts):
        return self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                                 batch_size=32, show_progress_bar=False).astype('float32')

    def query(self, text):
        return self.documents(['为这个句子生成表示以用于检索相关文章：'+text])[0]


def load_documents():
    return json.loads((ROOT/'data/documents.json').read_text(encoding='utf-8'))


def acl_key(document):
    value = document['tenant']+'|'+','.join(sorted(document['roles']))
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class MemoryStore:
    """Exact in-memory baseline, used by unit tests. Not called HNSW."""
    backend = 'memory-exact'

    def __init__(self, documents, vectors, embedder):
        self.docs, self.vectors, self.embedder = documents, np.asarray(vectors), embedder

    def search(self, question, actor, k=5):
        # Apply ACL BEFORE vector similarity; unauthorized vectors are not scored.
        indices = [i for i, d in enumerate(self.docs) if actor.can_read(d)]
        if not indices:
            return []
        query = self.embedder.query(question)
        scores = self.vectors[indices] @ query
        ordered = sorted(zip(indices, scores), key=lambda pair: pair[1], reverse=True)[:k]
        return [Hit(d['id'], d['title'], d['text'], float(score), d.get('product_id'),
                    d.get('sources',[]), d.get('review_flags',[]),d['tenant'],d['roles'])
                for i, score in ordered for d in [self.docs[i]]]

    def authorized_documents(self, actor):
        return [Hit(d['id'],d['title'],d['text'],0,d.get('product_id'),
                    d.get('sources',[]),d.get('review_flags',[]),d['tenant'],d['roles'])
                for d in self.docs if actor.can_read(d)]


class PostgresStore:
    backend = 'pgvector-hnsw-acl-partitions'

    def __init__(self, url, embedder):
        self.url, self.embedder = url, embedder
        self.lock = threading.RLock()
        with self.connect() as connection:
            version=connection.execute('SELECT version() AS value').fetchone()['value']
        self.serial_queries='PGlite' in version

    def connect(self):
        # Also works with transaction-pooled or single-connection local proxies.
        return psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10, prepare_threshold=None)

    def load(self, documents, vectors):
        vectors = np.asarray(vectors)
        dimension = int(vectors.shape[1])
        if dimension != 512:
            raise ValueError('This demo schema expects a 512-dimensional embedding model')
        groups = {acl_key(d) for d in documents}
        with self.lock, self.connect() as connection:
            connection.execute('CREATE EXTENSION IF NOT EXISTS vector')
            connection.execute('''CREATE TABLE IF NOT EXISTS presales_documents (
                acl_key text NOT NULL, id text NOT NULL, tenant text NOT NULL,
                roles text[] NOT NULL, title text NOT NULL, body text NOT NULL,
                product_id text, embedding vector(512) NOT NULL,
                PRIMARY KEY (acl_key, id)
            ) PARTITION BY LIST (acl_key)''')
            connection.execute("ALTER TABLE presales_documents ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb")
            for group in sorted(groups):
                connection.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} PARTITION OF presales_documents FOR VALUES IN ({})').format(
                    sql.Identifier('presales_acl_'+group), sql.Literal(group)))
            connection.execute('CREATE INDEX IF NOT EXISTS presales_hnsw ON presales_documents USING hnsw (embedding vector_cosine_ops)')
            connection.execute('DELETE FROM presales_documents')
            with connection.cursor() as cursor:
                cursor.executemany('''INSERT INTO presales_documents
                    (acl_key,id,tenant,roles,title,body,product_id,embedding,metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s::jsonb)''',
                    [(acl_key(d), d['id'], d['tenant'], d['roles'], d['title'], d['text'],
                      d.get('product_id'), json.dumps(v.tolist()),
                      json.dumps({'sources':d.get('sources',[]),'review_flags':d.get('review_flags',[])},ensure_ascii=False))
                     for d, v in zip(documents, vectors)])
            connection.execute('ANALYZE presales_documents')

    def search(self, question, actor, k=5):
        # Discover only whole ACL groups this caller is entitled to read.
        with self.lock if self.serial_queries else nullcontext():
            query = json.dumps(self.embedder.query(question).tolist())
            with self.connect() as connection:
                connection.execute("SET LOCAL statement_timeout='10s'")
                groups = connection.execute('''SELECT DISTINCT acl_key FROM presales_documents
                    WHERE tenant IN ('*', %s) AND %s = ANY(roles)''', (actor.tenant, actor.role)).fetchall()
                connection.execute('SET LOCAL hnsw.ef_search = 80')
                results = []
                for group in groups:
                    results.extend(connection.execute('''SELECT id,title,body,product_id,metadata,tenant,roles,
                        1-(embedding <=> %s::vector) AS score FROM presales_documents
                        WHERE acl_key=%s AND tenant IN ('*',%s) AND %s=ANY(roles)
                        ORDER BY embedding <=> %s::vector LIMIT %s''',
                        (query, group['acl_key'], actor.tenant, actor.role, query, k)).fetchall())
        ordered = sorted(results, key=lambda row: row['score'], reverse=True)[:k]
        return [Hit(r['id'], r['title'], r['body'], float(r['score']), r['product_id'],
                    r['metadata'].get('sources',[]),r['metadata'].get('review_flags',[]),r['tenant'],r['roles']) for r in ordered]

    def explain_index(self, actor, question):
        with self.lock:
            vector = json.dumps(self.embedder.query(question).tolist())
            with self.connect() as connection:
                group = connection.execute('''SELECT acl_key FROM presales_documents
                    WHERE tenant IN ('*',%s) AND %s=ANY(roles) LIMIT 1''', (actor.tenant, actor.role)).fetchone()
                connection.execute('SET LOCAL enable_seqscan=off')
                connection.execute('SET LOCAL enable_bitmapscan=off')
                connection.execute('SET LOCAL enable_sort=off')
                return connection.execute('''EXPLAIN (FORMAT JSON) SELECT id FROM presales_documents
                    WHERE acl_key=%s AND tenant IN ('*',%s) AND %s=ANY(roles)
                    ORDER BY embedding <=> %s::vector LIMIT 5''',
                    (group['acl_key'], actor.tenant, actor.role, vector)).fetchone()['QUERY PLAN']

    def authorized_documents(self, actor):
        # BM25 corpus is selected by SQL permissions before tokenization or scoring.
        with self.lock if self.serial_queries else nullcontext():
            with self.connect() as connection:
                connection.execute("SET LOCAL statement_timeout='10s'")
                rows=connection.execute('''SELECT id,title,body,product_id,metadata,tenant,roles
                    FROM presales_documents WHERE tenant IN ('*',%s) AND %s=ANY(roles)
                    ORDER BY id''',(actor.tenant,actor.role)).fetchall()
        return [Hit(r['id'],r['title'],r['body'],0,r['product_id'],r['metadata'].get('sources',[]),
                    r['metadata'].get('review_flags',[]),r['tenant'],r['roles']) for r in rows]


def cached_vectors(embedder, directory):
    docs = load_documents()
    directory.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256((json.dumps(docs, sort_keys=True)+embedder.model_name).encode()).hexdigest()
    file = directory/('embeddings-'+fingerprint[:16]+'.npy')
    if file.exists():
        return docs, np.load(file, allow_pickle=False)
    vectors = embedder.documents([d['title']+'\n'+d['text'] for d in docs])
    np.save(file, vectors)
    return docs, vectors


def guarded_search(store,question,actor,metrics,k=5):
    metrics.retrievals.inc()
    hits=store.search(question,actor,k)
    metrics.retrieval_candidates.inc(len(hits))
    rejected=[h for h in hits if h.tenant is None or h.roles is None
              or not actor.can_read({'tenant':h.tenant,'roles':h.roles})]
    metrics.retrieval_unauthorized.inc(len(rejected))
    if rejected:
        metrics.denied.labels('retrieval-policy').inc()
        raise PermissionError('Retrieved records failed the independent permission audit')
    return hits
