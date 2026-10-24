import numpy as np
import pytest

from presales.auth import Actor
from presales.hybrid import HybridStore
from presales.retrieval import MemoryStore, Hit

ACTOR = Actor('sales','demo-a','sales')


class Embedder:
    def query(self, text):
        return np.array([1.,0.])


def document(id, text, tenant='demo-a', roles=None):
    return {'id':id,'title':id,'text':text,'tenant':tenant,'roles':roles or ['sales'],'product_id':None}


def test_bm25_recovers_keyword_candidate_missing_from_dense_then_reranks():
    docs=[document('dense','家用电池容量需求'),document('other','安装方式与位置'),
          document('keyword','EP11 8kW')]
    base=MemoryStore(docs,[[1,0],[.9,0],[.1,0]],Embedder())
    class Reranker:
        def scores(self, question, hits):
            assert {h.id for h in hits}=={'dense','keyword'}
            return [.99 if h.id=='keyword' else .1 for h in hits]
    store=HybridStore(base,Reranker(),candidates=2)
    hits=store.search('EP11',ACTOR,k=1)
    assert hits[0].id=='keyword' and hits[0].score==.99


def test_both_paths_and_reranker_exclude_other_tenant_and_role_before_scoring():
    docs=[document('allowed','EP11'),document('foreign','secret other-tenant EP11','demo-b'),
          document('manager','secret manager-only EP11',roles=['manager'])]
    base=MemoryStore(docs,[[.1,0],[1,0],[.9,0]],Embedder())
    class Reranker:
        def scores(self, question, hits):
            assert [h.id for h in hits]==['allowed']
            return [.9]
    assert [h.id for h in HybridStore(base,Reranker()).search('secret EP11',ACTOR)]==['allowed']


def test_broken_store_cannot_send_unauthorized_text_to_reranker():
    class Store:
        backend='broken'
        def search(self,*args):return []
        def authorized_documents(self,actor):
            return [Hit('private','secret','private content',0,None,tenant='demo-b',roles=['sales'])]
    class Reranker:
        def scores(self,*args):raise AssertionError('must not be called')
    with pytest.raises(PermissionError):
        HybridStore(Store(),Reranker()).search('anything',ACTOR)


def test_reranker_error_propagates_to_existing_workflow_fallback():
    base=MemoryStore([document('allowed','EP11')],[[1,0]],Embedder())
    class Reranker:
        def scores(self,*args):raise TimeoutError('model unavailable')
    with pytest.raises(TimeoutError):
        HybridStore(base,Reranker()).search('EP11',ACTOR)
