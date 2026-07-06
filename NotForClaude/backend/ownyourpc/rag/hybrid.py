"""Hybrid retrieval: BM25 (lexical) + vector (semantic), fused with RRF.

Self-contained BM25 (no external dependency, avoids Python-3.14 wheel
issues). Korean-friendly tokenizer: whitespace word tokens plus CJK
character bigrams so that agglutinative/compound Korean terms still match.
Reciprocal Rank Fusion combines the two rankings without score
normalization headaches.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Tuple

_WORD = re.compile(r"[0-9a-zA-Z]+|[가-힣]+")  # latin/num runs + hangul runs


def tokenize(text: str) -> List[str]:
    text = text.lower()
    toks: List[str] = []
    for m in _WORD.findall(text):
        if "가" <= m[0] <= "힣":     # hangul run -> char bigrams (+unigrams)
            if len(m) == 1:
                toks.append(m)
            else:
                toks.extend(m[i:i+2] for i in range(len(m) - 1))
        else:
            toks.append(m)
    return toks


class BM25:
    """BM25 Okapi. Fit once over a corpus, then score queries."""

    def __init__(self, corpus_tokens: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = corpus_tokens
        self.N = len(corpus_tokens)
        self.doc_len = [len(d) for d in corpus_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.tf: List[Counter] = [Counter(d) for d in corpus_tokens]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        # idf with +1 smoothing (always positive)
        self.idf: Dict[str, float] = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()
        }

    def scores(self, query: str) -> List[float]:
        q = tokenize(query)
        out = [0.0] * self.N
        for i in range(self.N):
            tf, dl = self.tf[i], self.doc_len[i]
            s = 0.0
            for term in q:
                f = tf.get(term)
                if not f:
                    continue
                idf = self.idf.get(term, 0.0)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * (f * (self.k1 + 1)) / denom
            out[i] = s
        return out

    def top(self, query: str, n: int) -> List[Tuple[int, float]]:
        scored = list(enumerate(self.scores(query)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s in scored[:n] if s > 0]


def reciprocal_rank_fusion(rank_lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    """Fuse multiple ranked ID lists. rank_lists[j] is IDs best-first."""
    fused: Dict[str, float] = {}
    for ids in rank_lists:
        for rank, _id in enumerate(ids):
            fused[_id] = fused.get(_id, 0.0) + 1.0 / (k + rank + 1)
    return fused
