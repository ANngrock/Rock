"""Качество поиска как код (F9): гибрид, fusion, rerank и метрика, которую нельзя уронить.

Поиск «на глаз» умирал так: скоринг правят, порог косинуса подвигают, «находит не то» становится
ощущением, а не регрессией. Здесь у каждого решения есть число:

* **BM25** (детерминированный lexical) и векторная ветка сливаются через **reciprocal-rank
  fusion** (``1/(k+rank)``) — без магии сравнения несопоставимых шкал; trigram/tsvector-ветки
  живут в SQL (``NotesRepo.search``), в этот модуль они попадают рангами;
* **rerank только над топ-N** (по умолчанию 20), не над всем корпусом: дешёвый
  :class:`LexicalReranker` (покрытие фразы, совпадение по заголовку, небольшой буст свежести) —
  офлайн-воспроизводимый; модельный реранк включается отдельно и платно;
* **nDCG@k и recall@k** (:func:`ndcg_at`, :func:`recall_at`) считаются над
  ``evals/retrieval_v1.jsonl`` в CI: любое изменение скоринга обязано не уронить пол.
  Пороги зафиксированы в eval-раннере, не «в голове»;
* **ANN — решение, а не рефлекс**: :func:`ann_gate` возвращает «индексировать можно» только при
  выполненной паре «recall ≥ X и p95 ≤ Y». Без измерения hnsw на 2048 измерениях нельзя и не нужно.

Модуль чистый и без БД: ровно поэтому evals гоняет его на каждом push, а SQL-слой вызывает те
же функции, что проверяются офлайн, — «что считается» и «что ранжирует» не разъезжаются.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "AnnGateVerdict",
    "LexicalReranker",
    "Reranker",
    "ann_gate",
    "bm25_rank",
    "dcg",
    "fuse_rrf",
    "ndcg_at",
    "recall_at",
    "tokenize",
    "truncate_vector",
]

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_STOPWORDS = frozenset(
    "и в по для как что это на с не о у над от из к ли ж well the a an of and or to in for".split()
)


def tokenize(text: str) -> list[str]:
    return [
        m.group(0).lower() for m in _TOKEN_RE.finditer(text or "") if m.group(0) not in _STOPWORDS
    ]


@dataclass(frozen=True, slots=True)
class Doc:
    doc_id: str
    title: str
    body: str = ""

    @property
    def text(self) -> str:
        return f"{self.title} {self.body}"


# ---------------------------------------------------------------- BM25


def bm25_rank(
    query: str, docs: Sequence[Doc], *, k1: float = 1.5, b: float = 0.75, limit: int = 40
) -> list[tuple[str, float]]:
    """Okapi BM25 поверх инвертированного подсчёта. Детерминированно: тай-брейк — по doc_id."""
    tokens = tokenize(query)
    if not tokens or not docs:
        return []
    doc_tokens = [tokenize(doc.text) for doc in docs]
    lengths = [len(toks) for toks in doc_tokens]
    avg_len = (sum(lengths) / len(lengths)) or 1.0
    df: Counter[str] = Counter()
    for toks in doc_tokens:
        df.update(set(toks))
    n_docs = len(docs)
    scored: list[tuple[str, float]] = []
    for index, toks in enumerate(doc_tokens):
        tf = Counter(toks)
        score = 0.0
        for term in tokens:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log(1.0 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * lengths[index] / avg_len))
        if score > 0:
            scored.append((docs[index].doc_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]


# ---------------------------------------------------------------- fusion


def fuse_rrf(rankings: Sequence[Sequence[str]], *, k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion: сумма ``1/(k+rank)`` по всем спискам. Ранг начинается с 1.

    ``k=60`` — из оригинальной статьи; он же и сглаживает «первое место одной системы»: с
    маленьким k гибрид вырождается в «кто громче», с огромным — в усреднение хвостов.
    """
    scores: dict[str, float] = {}
    for ranked in rankings:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    out = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return out


# ---------------------------------------------------------------- rerank


class Reranker:
    """Порт реранкера: принимает запрос, кандидатов, возвращает новый порядок (id'ы)."""

    def rerank(self, query: str, docs: Sequence[Doc]) -> list[str]:  # pragma: no cover - протокол
        raise NotImplementedError


@dataclass(slots=True)
class LexicalReranker(Reranker):
    """Дешёвый реранк «последней мили»: покрытие запроса + совпадение в заголовке + свежесть.

    Не «вторая модель», а линейный скор: его можно объяснить владельцу, прогнать офлайн и
    вернуть к нему доверие после падения провайдера. Модельный реранк (LLM-as-reranker) —
    следующий шаг, и он включается явно (``RERANK_MODE=model``), потому что платный.
    """

    phrase_weight: float = 2.0
    title_weight: float = 1.5
    recency_weight: float = 0.2

    def rerank(
        self, query: str, docs: Sequence[Doc], *, created_at: Mapping[str, float] | None = None
    ) -> list[str]:
        tokens = tokenize(query)
        if not tokens:
            return [doc.doc_id for doc in docs]
        query_set = set(tokens)
        stamp = created_at or {}
        scored: list[tuple[float, str]] = []
        for doc in docs:
            toks = set(tokenize(doc.text))
            title_toks = set(tokenize(doc.title))
            coverage = len(query_set & toks) / len(query_set)
            title_hits = len(query_set & title_toks) / len(query_set)
            recency = min(max(stamp.get(doc.doc_id, 0.0), 0.0), 1.0)
            score = (
                self.phrase_weight * coverage
                + self.title_weight * title_hits
                + self.recency_weight * recency
            )
            scored.append((score, doc.doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [doc_id for _, doc_id in scored]


# ---------------------------------------------------------------- метрики качества


def dcg(relevances: Sequence[float]) -> float:
    return sum(rel / math.log2(index + 2.0) for index, rel in enumerate(relevances))


def ndcg_at(ranked_ids: Sequence[str], relevant: Mapping[str, float], k: int = 10) -> float:
    """nDCG@k с graded relevance (0/1/2/3). Идеальный порядок считается по тем же меткам."""
    gains = [float(relevant.get(doc_id, 0.0)) for doc_id in ranked_ids[:k]]
    ideal = sorted((float(v) for v in relevant.values()), reverse=True)[:k]
    if not ideal or ideal[0] <= 0:
        return 0.0
    return dcg(gains) / dcg(ideal)


def recall_at(ranked_ids: Sequence[str], relevant: Iterable[str], k: int = 10) -> float:
    wanted = {str(rid) for rid in relevant}
    if not wanted:
        return 1.0  # «всё найденное релевантно» — пустое множество требований не ухудшает recall
    hits = sum(1 for rid in ranked_ids[:k] if rid in wanted)
    return hits / len(wanted)


# ---------------------------------------------------------------- ANN-гейт


@dataclass(frozen=True, slots=True)
class AnnGateVerdict:
    allow: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        if self.allow:
            return "ANN-индексирование одобрено"
        return "ANN отложено: " + "; ".join(self.reasons)


def ann_gate(
    *,
    recall_at_k: float,
    p95_latency_ms: float,
    min_recall: float = 0.95,
    max_p95_ms: float = 25.0,
    measured: bool = True,
    dims: int = 2048,
) -> AnnGateVerdict:
    """Можно ли ANN (hnsw/halfvec). Нет измерения — нельзя, как бы ни хотелось.

    Отдельная функция потому, что соблазн «добавим индекс, раз он дешевле» вечен: exact scan
    на личном корпусе стоит миллисекунды, а hnsw на 2048 измерениях в pgvector физически
    недоступен — без сокращения размерности (Matryoshka→1024) или halfvec разговор не о выборе,
    а о фантазии. Гейт делает «измерил → включил» единственным порядком действий.
    """
    reasons: list[str] = []
    if not measured:
        reasons.append("нет замеров recall/latency на реальном корпусе")
    if dims > 2000:  # noqa: PLR2004 — лимит hnsw в pgvector, не наша прихоть
        reasons.append(
            f"размерность {dims} > 2000: hnsw невозможен; нужен halfvec или Matryoshka-усечение"
        )
    if recall_at_k < min_recall:
        reasons.append(f"recall@k={recall_at_k:.3f} ниже порога {min_recall:.3f}")
    if p95_latency_ms > max_p95_ms:
        reasons.append(f"p95={p95_latency_ms:.1f}мс выше потолка {max_p95_ms:.1f}мс")
    return AnnGateVerdict(allow=not reasons, reasons=tuple(reasons))


def truncate_vector(vector: Sequence[float], dims: int) -> list[float]:
    """Matryoshka-усечение с перевнормировкой: первые N координат embedding-3 сохраняют смысл.

    Перенормировка обязательна: косинусное расстояние на усечённом векторе без неё смещено,
    и «recall упал после усечения» оказалось бы арифметикой, а не свойством данных.
    """
    head = [float(v) for v in list(vector)[:dims]]
    norm = math.sqrt(sum(v * v for v in head)) or 1.0
    return [v / norm for v in head]


def hybrid_search(
    query: str,
    docs: Sequence[Doc],
    *,
    vector_ranked: Sequence[str] = (),
    k: int = 60,
    candidates: int = 40,
    rerank: Reranker | None = None,
    rerank_limit: int = 20,
) -> list[tuple[str, float]]:
    """Полный путь: lexical + vector → RRF → rerank только топ-``rerank_limit``.

    Реранк над всем корпусом — это O(N) работы ради порядка, который никто не дочитает после
    десятой строки; ограничение топ-N — не оптимизация, а контракт «дорогая стадия смотрит на
    мало кандидатов» (тот же принцип, что у «сначала фильтр, потом модель»).
    """
    lexical = [doc_id for doc_id, _ in bm25_rank(query, docs, limit=candidates)]
    fused = fuse_rrf([lexical, list(vector_ranked)], k=k)[:candidates]
    order = {doc_id: position for position, (doc_id, _score) in enumerate(fused)}
    if rerank is not None and order:
        by_id = {doc.doc_id: doc for doc in docs}
        head = [by_id[doc_id] for doc_id, _ in fused[:rerank_limit] if doc_id in by_id]
        reshaped = rerank.rerank(query, head)
        for position, doc_id in enumerate(reshaped):
            order[doc_id] = min(order.get(doc_id, position), position)
    ordered = sorted(order.items(), key=lambda item: item[1])
    top = int(candidates)
    return [(doc_id, 1.0 / (i + 1)) for i, (doc_id, _pos) in enumerate(ordered[:top])]
