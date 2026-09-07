"""Карманные вычисления: калькулятор и конвертер единиц — без LLM-галлюцинаций в арифметике.

Зачем отдельный инструмент, если модель «сама умеет считать»: модель умеет — но не всегда,
и не объясняет, где ошиблась. 17×23 модель может споткнуться, а «9**9**9» может положить
процесс. Поэтому: выражение разбирает AST-парсер с белым списком операций (никакого eval),
степень ограничена, таблица конверсии — детерминированная константа модуля. Тесты покрывают
векторы; то, что может проверить кодом, не отдаётся на волю генерации.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.tools.registry import ToolContext, registry
from aegis.governance.policy import Risk

__all__ = ["KNOWN_UNITS", "calc_value", "convert_value"]

_MAX_EXPR = 500
_MAX_POW = 512  # потолок показателя: 9**9**9 у нас не считают даже в шутку

_BIN_OPS: dict[type[Any], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[Any], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}


def calc_value(expression: str) -> float:
    """Посчитать арифметическое выражение. Ограниченный AST, никакого eval."""
    expr = expression.strip().replace(",", ".", 1) if expression.count(",") == 1 else expression
    if len(expr) > _MAX_EXPR:
        raise ValueError(f"слишком длинное выражение (макс {_MAX_EXPR})")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"не арифметика: {exc.msg}") from exc
    return _eval_expr(tree.body)


def _eval_expr(node: ast.expr) -> float:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError("такая операция запрещена")
        left, right = _eval_expr(node.left), _eval_expr(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > _MAX_POW or (abs(left) > 1e6 and abs(right) > 12):
                raise ValueError("показатель степени слишком большой")
        if isinstance(node.op, ast.Mod | ast.Div | ast.FloorDiv) and right == 0:
            raise ValueError("деление на ноль")
        return float(op(left, right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("такая операция запрещена")
        return float(op(_eval_expr(node.operand)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("разрешены только функции из списка описания инструмента")
        if node.keywords:
            raise ValueError("именованные аргументы не нужны")
        args = [_eval_expr(a) for a in node.args]
        fn = _FUNCS[node.func.id]
        try:
            return (
                float(fn(*args))
                if node.func.id not in ("round", "floor", "ceil")
                else float(
                    fn(*args) if node.func.id != "round" or len(args) > 1 else round(args[0])
                )
            )
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"аргументы не подходят: {exc}") from exc
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"неизвестное имя «{node.id}»")
    raise ValueError("такой синтаксис не считаю")


# ---------- конвертер единиц ----------
#: factor к базовой единице домена; домен выбирается по любому из двух имён
_KNOWN: dict[str, dict[str, float]] = {
    "length": {
        "mm": 0.001,
        "cm": 0.01,
        "m": 1.0,
        "km": 1000.0,
        "in": 0.0254,
        "inch": 0.0254,
        "ft": 0.3048,
        "foot": 0.3048,
        "yd": 0.9144,
        "mi": 1609.344,
        "nmi": 1852.0,
    },
    "mass": {
        "mg": 1e-06,
        "g": 0.001,
        "kg": 1.0,
        "t": 1000.0,
        "tonne": 1000.0,
        "oz": 0.028349523125,
        "lb": 0.45359237,
        "pound": 0.45359237,
        "st": 6.35029318,
    },
    "volume": {
        "ml": 0.001,
        "l": 1.0,
        "m3": 1000.0,
        "tsp": 0.00492892159375,
        "tbsp": 0.01478676478125,
        "cup": 0.24,
        "pt": 0.473176473,
        "qt": 0.946352946,
        "gal": 3.785411784,
    },
    "data": {
        "b": 1.0,
        "kb": 1024.0,
        "mb": 1024.0**2,
        "gb": 1024.0**3,
        "tb": 1024.0**4,
        "kib": 1024.0,
        "mib": 1024.0**2,
        "gib": 1024.0**3,
        "tib": 1024.0**4,
        "kbyte": 1000.0,
        "mbyte": 1000.0**2,
        "gbyte": 1000.0**3,
    },
    "time": {
        "ms": 0.001,
        "s": 1.0,
        "sec": 1.0,
        "min": 60.0,
        "h": 3600.0,
        "hour": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "wk": 604800.0,
        "week": 604800.0,
    },
    "speed": {"m/s": 1.0, "km/h": 1 / 3.6, "kph": 1 / 3.6, "mph": 0.44704, "kn": 0.5144},
    "area": {
        "m2": 1.0,
        "km2": 1e6,
        "ha": 1e4,
        "acre": 4046.8564224,
        "ft2": 0.09290304,
    },
    "angle": {"deg": 1.0, "rad": 180.0 / math.pi, "grad": 0.9, "turn": 360.0},
    "pressure": {
        "pa": 1.0,
        "kpa": 1000.0,
        "mpa": 1e6,
        "bar": 1e5,
        "hpa": 100.0,
        "psi": 6894.757293168,
        "mmhg": 133.322387415,
        "atm": 101325.0,
    },
}
_DOMAIN_OF: dict[str, str] = {unit: domain for domain, table in _KNOWN.items() for unit in table}
_TEMP = ("c", "celsius", "f", "fahrenheit", "k", "kelvin")


def convert_value(value: float, src: str, dst: str) -> float:
    """Перевести единицы. Таблица одна, детерминированная; температура — отдельная формула."""
    s, d = src.strip().lower().rstrip("."), dst.strip().lower().rstrip(".")
    if s in _TEMP or d in _TEMP:
        if s not in _TEMP or d not in _TEMP:
            raise ValueError("температура переводится только между C/F/K")
        return _convert_temp(value, s, d)
    ds, dd = _DOMAIN_OF.get(s), _DOMAIN_OF.get(d)
    if ds is None or dd is None:
        unknown = s if ds is None else d
        raise ValueError(f"единица «{unknown}» неизвестна; знаю: {', '.join(sorted(_DOMAIN_OF))}")
    if ds != dd:
        raise ValueError(f"«{s}» и «{d}» — про разные вещи ({ds} vs {dd})")
    table = _KNOWN[ds]
    return float(value) * table[s] / table[d]


def _convert_temp(v: float, s: str, d: str) -> float:
    c = {
        "c": v,
        "celsius": v,
        "f": (v - 32.0) * 5.0 / 9.0,
        "fahrenheit": (v - 32.0) * 5 / 9,
        "k": v - 273.15,
        "kelvin": v - 273.15,
    }[s]
    return {
        "c": c,
        "celsius": c,
        "f": c * 9.0 / 5.0 + 32.0,
        "fahrenheit": c * 9 / 5 + 32.0,
        "k": c + 273.15,
        "kelvin": c + 273.15,
    }[d]


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.10g}"


KNOWN_UNITS = ", ".join(sorted(_DOMAIN_OF))


class CalcArgs(BaseModel):
    expression: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "арифметическое выражение: + - * / // % ** , скобки, функции "
            "sqrt/exp/log/log10/log2/sin/cos/tan/asin/acos/atan/abs/round/min/max/floor/ceil, "
            "константы pi/e/tau. Без переменных и без текста — только математика"
        ),
    )


class ConvertArgs(BaseModel):
    value: float = Field(description="число")
    from_unit: str = Field(min_length=1, max_length=12, description=f"из чего; знаю: {KNOWN_UNITS}")
    to_unit: str = Field(min_length=1, max_length=12, description="во что (тот же алфавит)")


@registry.register(
    "calc",
    "Посчитать арифметическое выражение ТОЧНО (код, не «на глазок»). Используй для любой "
    "нетривиальной математики, процентов, финансов — модель обязана звать это, а не гадать.",
    CalcArgs,
    risk=Risk.NONE,
)
async def calc(args: CalcArgs, ctx: ToolContext) -> str:  # noqa: ARG001
    try:
        result = calc_value(args.expression)
    except (ValueError, ZeroDivisionError) as exc:
        return f"Не считаю: {exc}"
    return f"{args.expression.strip()} = {_fmt(result)}"


@registry.register(
    "convert",
    "Перевести единицы точно по таблице: длина, масса, объём, данные, время, скорость, "
    "площадь, угол, давление, температура C/F/K. Единицы — коротко (km, lb, gal, GiB); "
    "полный список — в описании поля from_unit и в тексте ошибки.",
    ConvertArgs,
    risk=Risk.NONE,
)
async def convert(args: ConvertArgs, ctx: ToolContext) -> str:  # noqa: ARG001
    try:
        out = convert_value(args.value, args.from_unit, args.to_unit)
    except ValueError as exc:
        return f"Не перевожу: {exc}"
    src_u, dst_u = args.from_unit.strip().lower(), args.to_unit.strip().lower()
    return f"{_fmt(args.value)} {src_u} = {_fmt(out)} {dst_u}"
