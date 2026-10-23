import os
from pathlib import Path

# This local portfolio demo never sends traces/prompts to a hosted tracing service.
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'

from presales.api import create_app
from presales.model import LocalModel
from presales.observability import Metrics
from presales.retrieval import Embedder, MemoryStore, PostgresStore, cached_vectors

ROOT = Path(__file__).resolve().parent


def build_app():
    metrics = Metrics()
    embedder = Embedder(os.getenv('PRESALES_EMBED_MODEL', 'BAAI/bge-small-zh-v1.5'))
    docs, vectors = cached_vectors(embedder, ROOT/'.storage')
    url = os.getenv('PRESALES_DATABASE_URL')
    if url:
        store = PostgresStore(url, embedder)
        store.load(docs, vectors)
    else:
        store = MemoryStore(docs, vectors, embedder)
    mode = os.getenv('PRESALES_MODEL_MODE', 'extractive')
    if mode not in {'extractive', 'local'}:
        raise ValueError('PRESALES_MODEL_MODE must be extractive or local')
    model = LocalModel(os.getenv('PRESALES_LOCAL_MODEL', 'Qwen/Qwen3-4B-Instruct-2507'), metrics) if mode == 'local' else None
    return create_app(store, ROOT/'.storage', model, os.getenv('PRESALES_DEMO_MODE', '1') == '1', metrics)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(build_app(), host='127.0.0.1', port=8128)
