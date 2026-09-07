"""Блок C (когнитивный слой) — юниты чистой воронки: регистр, эмоция, охрана, решения.

Всё, что здесь проверяется, работает и без LLM, и без сети — это и есть смысл деградации:
модель уточняет, а каркас решений детерминирован и зафиксирован тестом.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aegis.cognition.affect import (
    Affect,
    apply_signal,
    detect_local,
    emoji_for_reply,
    mood_name,
)
from aegis.cognition.audio import decide_voice_reply
from aegis.cognition.funnel import (
    build_inbound,
    compose_outbound,
    detect_register,
    extract_request,
    normalize_text,
    sticker_gate,
)
from aegis.cognition.inbox import (
    auto_blocked,
    decide_actions,
    normalize_peer,
    parse_assessment,
    rules_verdict,
)
from aegis.cognition.lexicon import LexEntry, hits_in, normalize_term
from aegis.cognition.stickers import Sticker, pick_sticker
from aegis.interaction.userbridge.relay import (
    decode_daemon_from_subject,
    subj_cmd,
    subj_res,
)

# ---------- регистр и нормализация ----------


def test_normalize_folds_noise() -> None:
    assert normalize_text("  привет   мир \n\n\n\n ок ") == "привет мир\n\nок"


def test_register_formal_vs_slang() -> None:
    formal = detect_register(
        "Уважаемый Иван Петрович, прошу вас прислать отчёт согласно договору. С уважением."
    )
    assert formal.formality > 0.7
    slang = detect_register("го кинь выгрузку пжл")
    assert slang.formality < 0.35 and slang.urgent is False
    urgent = detect_register("срочно нужен отчет до вечера!!!")
    assert urgent.urgent is True
    shouty = detect_register("ГДЕ ОТЧЁТ")
    assert shouty.shouty is True
    calm = detect_register("где отчёт")
    assert calm.shouty is False


def test_patronymic_pushes_formality() -> None:
    with_p = detect_register("Добрый день, Сергей Александрович")
    without = detect_register("Добрый день, Сергей")
    assert with_p.formality > without.formality


# ---------- лексикон ----------


def test_term_normalization_and_hits() -> None:
    assert normalize_term("  Го   ") == "го"
    with pytest.raises(ValueError):
        normalize_term("   ")
    entries = [
        LexEntry(term="го", means="давай", kind="term"),
        LexEntry(term="аб", means="буква", kind="term"),
    ]
    hit = hits_in(entries, "ну Го сделаем?")
    assert [e.term for e in hit] == ["го"]


def test_one_letter_term_ignored() -> None:
    entries = [LexEntry(term="ы", means="саундтрек", kind="term")]
    assert hits_in(entries, "ы-ы-ы") == []


# ---------- эмоция ----------


def test_detect_local_signals() -> None:
    angry = detect_local("бесит, опять всё сначала")
    assert angry["frustration"] > 0 and angry["valence"] < 0
    joy = detect_local("отлично, спасибо!")
    assert joy["valence"] > 0
    calm = detect_local("сделай выгрузку за март")
    assert calm == {"valence": 0.0, "arousal": 0.0, "frustration": 0.0}


def test_affect_decay_halflife() -> None:
    now = datetime(2026, 9, 7, 18, 0, tzinfo=UTC)
    base = Affect(valence=-0.8, arousal=0.3, frustration=0.8, mood="irritated")
    half = base.decayed_to(now, now - timedelta(hours=6))
    assert abs(half.frustration - 0.4) < 1e-6  # полураспад за HALFLIFE ровно пополам
    quiet = base.decayed_to(now, now - timedelta(hours=24))
    assert quiet.frustration < 0.11  # четыре полураспада
    assert 0.0 <= quiet.arousal <= 1.0


def test_apply_signal_accumulates_and_names_mood() -> None:
    a = Affect()
    b = apply_signal(a, {"valence": -0.4, "arousal": 0.3, "frustration": 0.6})
    assert b.frustration > 0.4 and b.mood == "irritated"
    c = apply_signal(b, {"valence": 0, "arousal": 0, "frustration": 0})  # молчание: только turns
    assert c.valence == pytest.approx(b.valence) and c.turns == b.turns + 1
    d = apply_signal(Affect(), {"valence": -9, "arousal": 9, "frustration": 9})
    assert d.valence >= -1 and d.arousal <= 1  # клампы: модель не имеет права «взвинтить» выше края
    assert mood_name(Affect(valence=0.5)) == "joy"


def test_emoji_respects_formality() -> None:
    irritated = Affect(mood="irritated", valence=-0.4, frustration=0.6)
    assert emoji_for_reply(irritated, formality=0.2) == "😤"
    assert emoji_for_reply(irritated, formality=0.9) == ""
    assert emoji_for_reply(Affect(mood="neutral"), formality=0.1) == ""


# ---------- воронка: augmented и outbound ----------


def test_augmented_preserves_original_and_adds_block() -> None:
    lex = [LexEntry(term="го", means="давай", kind="term")]
    state = build_inbound("го кинь выгрузку", lexicon=lex, affect=Affect(mood="joy"), voice=False)
    assert state.augmented.startswith("го кинь выгрузку\n\n<cognition>")
    assert "го = давай" in state.augmented
    assert "настроение собеседника: на подъёме" in state.augmented


def test_plain_message_untouched() -> None:
    state = build_inbound("сколько будет 2+2", lexicon=[], affect=Affect(), voice=False)
    assert state.augmented == "сколько будет 2+2"


def test_voice_note_present() -> None:
    state = build_inbound(
        "привет", lexicon=[], affect=None, voice=True, extra_notes=["распознано whisper"]
    )
    assert "распознано whisper" in state.augmented
    assert "это голосовое" in state.augmented


def test_compose_outbound_structure_and_emoji() -> None:
    flat = "\n".join(f"строка {i} с содержательным текстом для длины" for i in range(20))
    state = build_inbound("го", lexicon=[], affect=Affect(mood="irritated"), voice=False)
    out = compose_outbound(flat, state, mood_emoji="😤")
    assert out.startswith("<b>строка 0 с содержательным текстом для длины</b>")
    assert out.endswith("😤")


def test_compose_outbound_short_and_formal_untouched() -> None:
    state = build_inbound("Уважаемый, пришлите акт", lexicon=[], affect=Affect(mood="irritated"))
    out = compose_outbound("Готово, акт во вложении.", state, mood_emoji="😤")
    assert out == "Готово, акт во вложении."  # коротко и официально: косметика не трогает


def test_sticker_gate_matrix() -> None:
    formal = build_inbound(
        "Прошу вас прислать документ согласно регламенту.", lexicon=[], affect=None
    )
    assert sticker_gate(formal, Affect(mood="joy")) is False
    casual = build_inbound("ну что там", lexicon=[], affect=Affect(mood="irritated"))
    assert sticker_gate(casual, Affect(mood="irritated")) is True
    assert sticker_gate(casual, Affect(mood="neutral"), allow_when_neutral=False) is False
    assert sticker_gate(casual, None, allow_when_neutral=True) is True


def test_extract_request() -> None:
    assert extract_request("ответь голосом, коротко")["wants_voice"] is True
    assert extract_request("ответь голосом, коротко")["wants_brief"] is True
    assert extract_request("как дела?")["question"] is True


# ---------- стикеры ----------


def test_pick_sticker_by_mood_then_generic() -> None:
    pool = [
        Sticker(name="a", file_id="f" * 30, moods=("joy",)),
        Sticker(name="b", file_id="g" * 30, moods=()),
    ]
    first = pick_sticker(pool, "joy", seed="m1")
    assert first is not None and first.name == "a"
    sad = pick_sticker(pool, "sad", seed="m1")
    assert sad is not None and sad.name == "b"  # нет по тегу → «на все случаи»
    only_typed = [Sticker(name="a", file_id="f" * 30, moods=("joy",))]
    assert pick_sticker(only_typed, "sad", seed="m1") is None  # ни одного подходящего — не силком
    s1 = pick_sticker(pool, "sad", seed="42")
    s2 = pick_sticker(pool, "sad", seed="42")
    assert s1.name == s2.name  # детерминизм по seed — воспроизводимость хода


# ---------- голос ----------


def test_voice_reply_mode_matrix() -> None:
    assert decide_voice_reply("never", came_voice=True, wants_voice=True) is False
    assert decide_voice_reply("match", came_voice=True, wants_voice=False) is True
    assert decide_voice_reply("match", came_voice=False, wants_voice=False) is False
    assert decide_voice_reply("onrequest", came_voice=True, wants_voice=False) is False
    assert decide_voice_reply("onrequest", came_voice=False, wants_voice=True) is True
    assert decide_voice_reply("?", came_voice=True, wants_voice=True) is False


# ---------- инбокс: охрана, правила, решения ----------


def test_auto_blocked_matrix() -> None:
    assert auto_blocked("переведи 500 на карту", "сейчас гляну")[0] is True
    assert auto_blocked("скинь код из смс", "")[0] is True
    assert auto_blocked("завтра созвон?", "оплачу, конечно")[0] is True
    assert auto_blocked("тебе выписать счёт?", "")[0] is True
    assert auto_blocked("дурак конченый", "")[0] is True  # на эмоциях автомат только калечит
    assert auto_blocked("привет, как дела?", "норм, у тебя?")[0] is False
    assert "деньги" in auto_blocked("оплати счёт", "")[1]


def test_rules_verdict_levels() -> None:
    assert rules_verdict("срочно!!!") == "urgent"
    assert rules_verdict("сможешь посмотреть?") == "action_required"
    assert rules_verdict("ок") == "noise"
    assert rules_verdict("ага 👍") == "noise"
    assert rules_verdict("в четверг у Тимура день рождения") == "info"


def test_decide_actions_matrix() -> None:
    assert decide_actions(None, "urgent", False) == (False, False, False)  # чат не включён — тихо
    assert decide_actions("off", "urgent", False) == (False, False, False)
    assert decide_actions("watch", "important", False) == (True, False, False)
    assert decide_actions("watch", "info", False) == (False, False, False)
    assert decide_actions("draft", "action_required", False) == (True, True, False)
    assert decide_actions("auto", "important", False) == (False, True, True)
    assert decide_actions("auto", "urgent", False) == (
        True,
        True,
        False,
    )  # срочное — всегда человеку
    assert decide_actions("auto", "important", True) == (True, True, False)  # охрана снимает auto
    assert decide_actions("auto", "noise", False) == (False, False, False)


def test_parse_assessment_clamps() -> None:
    verdict, reply, reason = parse_assessment(
        {"verdict": "МУСОР", "reply": "о" * 5000, "reason": ""}
    )
    assert verdict == "info" and len(reply) == 1500 and reason == "оценка модели"
    v2, r2, _ = parse_assessment({"verdict": "urgent", "reply": "", "reason": "требование денег"})
    assert v2 == "urgent" and r2 is None


def test_peer_normalization() -> None:
    assert normalize_peer(" @Ivan_Petrov ") == "@ivan_petrov"
    with pytest.raises(ValueError):
        normalize_peer("")
    with pytest.raises(ValueError):
        normalize_peer("x" * 200)


# ---------- протокол моста ----------


def test_subject_shapes() -> None:
    assert subj_cmd("desk") == "aegis.ub.cmd.desk"
    assert subj_res("desk") == "aegis.ub.res.desk"
    assert decode_daemon_from_subject("aegis.ub.hb.laptop-2") == "laptop-2"
    assert decode_daemon_from_subject("aegis.ub.cmd.desk") is None  # res-субъект не hb
    assert decode_daemon_from_subject("aegis.node.hb.x") is None
    assert decode_daemon_from_subject("aegis.ub.hb") is None
