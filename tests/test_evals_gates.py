"""CI-гейты над evals/ и snapshot-контрактами: «не стало хуже» проверяется машиной.

Три независимых замка, каждый — на то место, где обычно тихо деградируют:

1. Флаги: заведение без golden-пары — это «ещё один if в коде, о котором забыли тесты».
2. Ранжирование: смена скоринга без замера = re-roll ранков вслепую. nDCG по размеченному
   датасету — тот же контракт, что unit-тест, только про порядок, а не про значения.
3. Контракт событий: snapshot released-схемы обязателен; tightening (ушёл тип, сузился
   required) = breaking = новая версия контракта, и здесь это падение CI, а не «перечитаем».

Миграционный guard (F10) рядом: переименование/удаление колонки, которую предыдущая ревизия
уже отдала продакшену, ломает откат — поэтому запрещено конвенцией и проверено текстом файлов.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import pytest

from aegis.knowledge.ranking import Doc, LexicalReranker, bm25_rank, fuse_rrf
from aegis.platform.flags import FLAG_CATALOG

ROOT = Path(__file__).resolve().parents[1]
NDCG_FLOOR = 0.94
CASE_FLOOR = 0.70


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ndcg(order: list[str], rel: set[str], k: int = 5) -> float:
    """nDCG@k с ideal по ВСЕМ релевантам датасета: пустой ответ ≠ «идеально»."""
    gains = [1.0 if d in rel else 0.0 for d in order[:k]]
    g = lambda xs: sum(v / math.log2(i + 2) for i, v in enumerate(xs))  # noqa: E731
    ideal = [1.0] * min(len(rel), k)
    d0 = g(ideal)
    return g(gains) / d0 if d0 else 1.0


# ------------------------------------------------------------------ флаги (F6)


def test_every_flag_has_golden_pair_in_both_states() -> None:
    cases = {c["id"]: c for c in _load_jsonl(ROOT / "evals" / "flags_v1.jsonl")}
    for key, spec in FLAG_CATALOG.items():
        off_id, on_id = spec.golden
        assert off_id and on_id, f"{key}: пустая golden-пара — флаг без проверки"
        for cid, state in ((off_id, "off"), (on_id, "on")):
            case = cases.get(cid)
            assert case is not None, f"{key}: нет golden-кейса {cid} в evals/flags_v1.jsonl"
            assert case["flag"] == key, f"{cid}: кейс записан под чужой флаг"
            assert case["state"] == state, f"{cid}: состояние должно совпадать с именем golden"


# ------------------------------------------------------------------ ранжирование (F9)


def _run_pipeline(case: dict[str, Any]) -> dict[str, list[str]]:
    corpus = {d["id"]: d for d in case["corpus"]}
    docs = [Doc(doc_id=i, title=d["title"], body=d["body"]) for i, d in corpus.items()]
    text_rank = [i for i, _ in bm25_rank(case["query"], docs, limit=len(docs))]
    fused = fuse_rrf([text_rank, case["vector_rank"]])
    top = [i for i, _ in fused[:20]]
    reranked = LexicalReranker().rerank(
        case["query"],
        [Doc(doc_id=i, title=corpus[i]["title"], body=corpus[i]["body"][:600]) for i in top],
    )
    return {"text": text_rank, "vector": case["vector_rank"], "hybrid": reranked}


@pytest.fixture(scope="module")
def search_cases() -> list[dict[str, Any]]:
    return _load_jsonl(ROOT / "evals" / "search_ranking_v1.jsonl")


def test_search_ranking_meets_ndcg_floor(search_cases: list[dict[str, Any]]) -> None:
    scores = []
    for case in search_cases:
        order = _run_pipeline(case)["hybrid"]
        score = _ndcg(order, set(case["relevant"]))
        assert score >= CASE_FLOOR, f"{case['id']}: nDCG@5={score:.3f} ниже порога кейса"
        scores.append(score)
    mean = sum(scores) / len(scores)
    assert mean >= NDCG_FLOOR, f"средний nDCG@5 {mean:.4f} < {NDCG_FLOOR} — скоринг деградировал"


def test_fusion_beats_text_on_lexical_gap(search_cases: list[dict[str, Any]]) -> None:
    """Ценность RRF-гибрида — кейс, где лексика слепнет; иначе гибрид = лишний код."""
    case = next(c for c in search_cases if c["id"] == "srch-synonym")
    out = _run_pipeline(case)
    assert _ndcg(out["text"], set(case["relevant"])) < 0.1, (
        "датасет перестал различать слепоту текста"
    )
    assert _ndcg(out["hybrid"], set(case["relevant"])) > 0.6, (
        "гибрид обязан вытягивать лексику-провал"
    )


def test_ranking_is_deterministic(search_cases: list[dict[str, Any]]) -> None:
    """Тот же вход — тот же порядок. Иначе A/B по флагам невозможно пересчитать."""
    for case in search_cases:
        assert _run_pipeline(case)["hybrid"] == _run_pipeline(case)["hybrid"]


# ------------------------------------------------------------------ контракт событий (F4)


def test_event_snapshot_stays_backward_compatible() -> None:
    """Released-контракт v1 — пол: новые версии могут добавлять, но не сужать.

    Правила совместимости ровно те, что обещаны в F4: тип не исчезает, required не
    расширяется (иначе старый продюсер «внезапно» стал невалидным), тип поля не меняется.
    Всё, что аддитивно, — живёт: реестр обязан только пережить переснапшот на следующий
    мажор, если кто-то решит, что «добавить required — это не breaking».
    """
    from aegis.platform.events.contracts import current_registry_export

    released = json.loads((ROOT / "schema" / "released" / "events-v1.json").read_text())
    raw = current_registry_export()
    current = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    cur_events: dict[str, Any] = current["events"]
    for name, spec in released["events"].items():
        assert name in cur_events, f"{name}: тип исчез из реестра — это breaking, готовьте v2"
        cur = cur_events[name]
        rel_payload, cur_payload = spec.get("payload") or {}, cur.get("payload") or {}
        rel_req, cur_req = (
            set(rel_payload.get("required") or []),
            set(cur_payload.get("required") or []),
        )
        assert rel_req <= cur_req, (
            f"{name}: обязательные поля v1 ({sorted(rel_req - cur_req)}) пропали — tightening"
        )
        rel_props = rel_payload.get("properties") or {}
        cur_props = cur_payload.get("properties") or {}
        for field, rel_def in rel_props.items():
            cur_def = cur_props.get(field)
            assert cur_def is not None, f"{name}: поле {field} исчезло из payload — breaking"
            assert cur_def.get("type") == rel_def.get("type"), (
                f"{name}.{field}: смена типа {rel_def.get('type')}→{cur_def.get('type')} = breaking"
            )
        assert int(cur.get("schema_version", 1)) >= int(spec.get("schema_version", 1))


# ------------------------------------------------------------------ guard миграций (F10)

#: расширение CHECK-констрейнта — не «drop колонки»: это единственный разрешённый способ
#: легализовать новое допустимое значение (0003/0004 так и делают); колонки же — святы
_ALLOWED_IN_UPGRADE = re.compile(
    r"\bDROP\s+COLUMN\b|\bRENAME\s+COLUMN\b|\bRENAME\s+TABLE\b|\bDROP\s+TABLE\b",
    re.IGNORECASE,
)


def _upgrade_blocks() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted((ROOT / "migrations" / "versions").glob("0*.py")):
        text = path.read_text(encoding="utf-8")
        m = re.search(r"(?ms)^def upgrade.*?(?=^def |\Z)", text)
        assert m, f"{path.name}: нет upgrade()"
        # только исполняемый SQL upgrade(): downgrade-блоки — наоборот, контракт-шаг, и
        # «DROP COLUMN» там — единственный легальный. Комментарии (--) не участвуют
        sql = "\n".join(ln for ln in m.group(0).splitlines() if not ln.strip().startswith("--"))
        out[path.name] = sql
    return out


def test_expand_phase_never_drops_or_renames() -> None:
    """Expand/contract: ни DROP, ни RENAME в upgrade() — ломается откат между релизами."""
    for name, sql in _upgrade_blocks().items():
        bad = _ALLOWED_IN_UPGRADE.findall(sql)
        assert not bad, (
            f"{name}: разрушительное изменение в upgrade() ({bad}) — унесите в contract-релиз"
        )


def test_migration_chain_is_linear_with_single_head() -> None:
    revs: dict[str, str | None] = {}
    for path in sorted((ROOT / "migrations" / "versions").glob("0*.py")):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision(?:: str)? = "([^"]+)"', text, re.M)
        down = re.search(r'^down_revision(?:: str \| None)? = (?:"([^"]+)"|None)', text, re.M)
        assert rev, f"{path.name}: нет revision"
        assert down, f"{path.name}: нет down_revision — alembic не соберёт цепь"
        revs[rev.group(1)] = down.group(1) if down.group(1) else None
    downs = {d for d in revs.values() if d}
    heads = [r for r in revs if r not in downs]
    assert len(revs) == len(set(revs)), "дубли revisions"
    assert heads == [max(heads)], f"один линейный head, найдено {heads}"
    assert "0007" in revs, "миграция алиаса (F9) обязана быть в цепи"
    missing = [d for d in revs.values() if d is not None and d not in revs]
    assert not missing, f"висячие down_revision: {missing} — цепь врёт, alembic упадёт на upgrade"
