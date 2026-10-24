"""ACL-scoped dense + BM25 retrieval, RRF fusion and cross-encoder reranking."""
import logging
import re
import threading
from dataclasses import replace

import jieba
from rank_bm25 import BM25Okapi

jieba.setLogLevel(logging.WARNING)


def tokenize(text):
    return [token for token in jieba.lcut_for_search(text.lower())
            if re.fullmatch(r'[\w.]+', token) and token not in {'的','是','和','请','什么','哪些'}]


class Reranker:
    model_name = 'BAAI/bge-reranker-base'

    def __init__(self):
        import torch
        from sentence_transformers import CrossEncoder
        self.model = CrossEncoder(self.model_name, max_length=512, device='cpu')
        self.activation = torch.nn.Sigmoid()
        self.lock = threading.Lock()

    def scores(self, question, hits):
        with self.lock:
            values = self.model.predict([(question,h.title+'\n'+h.text) for h in hits],
                batch_size=16, show_progress_bar=False, activation_fn=self.activation)
        return [float(value) for value in values]


class HybridStore:
    def __init__(self, store, reranker=None, candidates=20):
        self.store = store
        self.reranker = reranker if reranker is not None else Reranker()
        self.candidates = candidates
        self.backend = store.backend+'-bm25-rrf-rerank'

    def search(self, question, actor, k=5):
        if type(k) is not int or not 1 <= k <= 20:
            raise ValueError('检索条数必须是1到20之间的整数')
        dense = self.store.search(question,actor,max(k,self.candidates))
        documents = self.store.authorized_documents(actor)
        # Defense in depth: a broken store must not leak private text to BM25/reranker.
        if any(h.tenant is None or h.roles is None or
               not actor.can_read({'tenant':h.tenant,'roles':h.roles}) for h in dense+documents):
            raise PermissionError('Hybrid retrieval received unauthorized records')
        if not documents:
            return []
        # shortcut: rebuild BM25 for this small authorized catalog; index per ACL group when the corpus grows.
        tokens = [tokenize(h.title+'\n'+h.text) or ['_empty_'] for h in documents]
        sparse_scores = BM25Okapi(tokens).get_scores(tokenize(question))
        sparse = [documents[i] for i in sorted(range(len(documents)),key=lambda i:sparse_scores[i],reverse=True)
                  if sparse_scores[i]>0][:self.candidates]
        fused, by_id = {}, {}
        for ranked in [dense,sparse]:
            for rank,hit in enumerate(ranked,1):
                by_id[hit.id] = hit
                fused[hit.id] = fused.get(hit.id,0)+1/(60+rank)
        candidates = [by_id[id] for id in sorted(fused,key=lambda id:fused[id],reverse=True)[:self.candidates]]
        if not candidates:
            return []
        scores = self.reranker.scores(question,candidates)
        if len(scores)!=len(candidates) or any(not 0<=score<=1 for score in scores):
            raise ValueError('Invalid reranker scores')
        ranked = sorted(zip(candidates,scores),key=lambda item:item[1],reverse=True)
        return [replace(hit,score=score) for hit,score in ranked[:k]]
