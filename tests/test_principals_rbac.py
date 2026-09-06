"""RBAC-гранулы (F2): «можно ли» решается в одной точке, и эта точка покрыта тестами.

Самое опасное в грантах — не отказ, а молчаливый allow по недосмотру: инструмент без
маппинга получает пустое действие, пустое действие `allows()` считает «не требуется», и
платёж становится публичным. Поэтому здесь три страховки: таблица, семантика wildcard и
полнота маппинга над живым реестром.
"""

from __future__ import annotations

import pytest

from aegis.agents.tools import load_builtin_tools
from aegis.governance.principals import (
    KNOWN_ACTIONS,
    allows,
    default_grants,
    parse_roster,
    permission_for_tool,
)


def test_allows_semantics() -> None:
    assert allows(frozenset({"*"}), "anything") is True
    assert allows(frozenset(), "memory:write") is False
    assert allows(frozenset({"notes:read"}), "") is True, "действие '' не требует гранта"
    assert allows(frozenset({"notes:read"}), "notes:read") is True
    assert allows(frozenset({"notes:read"}), "notes:write") is False, (
        "не точечное совпадение — отказ"
    )


def test_guest_never_holds_anything_sensitive() -> None:
    guest = default_grants("guest")
    for action in ("tool:pay", "memory:write", "admin:policy", "admin:killswitch", "data:forget"):
        assert not allows(guest, action), f"гость не должен иметь {action}"
    assert allows(default_grants("member"), "notes:write")
    assert not allows(default_grants("member"), "tool:pay"), "pay — только владелец"


def test_parse_roster_ignores_garbage_pairs() -> None:
    roster = parse_roster("42:member,77:guest,0x1:member,,99:kingslayer,8:member")
    assert roster[42] == "member" and roster[8] == "member"
    assert 99 not in roster and 0 not in roster


def test_parse_roster_empty_is_empty() -> None:
    assert not parse_roster("")


@pytest.mark.parametrize(
    ("tool", "writes", "expected"),
    [
        ("add_note", True, "notes:write"),
        ("remember_fact", True, "memory:write"),
        ("search_notes", False, "notes:read"),
        ("finance.transfer", True, "tool:pay"),
        ("finance_pay", True, "tool:pay"),
        ("get_datetime", False, ""),
        ("anything_unknown", False, ""),
    ],
)
def test_permission_mapping(tool: str, writes: bool, expected: str) -> None:
    assert permission_for_tool(tool, writes=writes) == expected


def test_every_registered_write_tool_needs_a_grant() -> None:
    """Ни один writes-инструмент не может «не требовать» гранта — дырка именно так и рождается."""
    load_builtin_tools()
    from aegis.agents.tools import registry as app_registry

    tools = list(app_registry._tools.values())
    assert tools, "реестр обязан быть заполнен после load_builtin_tools"
    for spec in tools:
        if not spec.writes:
            continue
        required = permission_for_tool(spec.name, writes=True)
        assert required, f"{spec.name}: write-инструмент без требуемого гранта"
        assert required in KNOWN_ACTIONS, f"{spec.name}: грант {required} не описан в KNOWN_ACTIONS"
