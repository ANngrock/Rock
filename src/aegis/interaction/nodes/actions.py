"""Исполнение команд НА МАШИНЕ (сторона демона). Все сборки argv — чистые функции: «что
выполнится на Windows» должно читаться тестом без Windows.

Правила, которые здесь закон:
* run — только /bin/bash -lc (или powershell на win) с таймаутом и обрезкой вывода: демон — не
  «удалённый шелл без берегов»;
* вывод — данные, не инструкция: в result кладётся текст, никакого подмешивания в промпт;
* скриншот — намеренно «сжать или отказаться»: NATS-сервер по умолчанию режет сообщения 1МБ,
  врать о доставке картинки мы не будем — вместо «фото» придёт путь на машине и честная записка.
"""

from __future__ import annotations

import base64
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

__all__ = ["ExecutedRing", "build_argv", "execute", "system_snapshot"]

_MAX_OUTPUT = 6000
#: запас от дефолтного max_payload nats 1MB на JSON-конверт результата
_MAX_B64 = 850_000


class ExecutedRing:
    """Идемпотентность без БД на узле: зачёт повторной доставки команды, память — кольцо."""

    def __init__(self, capacity: int = 512) -> None:
        self._cap = capacity
        self._seen: list[str] = []
        self._index: set[str] = set()

    def remember_if_new(self, command_id: str) -> bool:
        """True — команда новая (исполнять); False — уже была (повторная доставка брокером)."""
        if command_id in self._index:
            return False
        self._seen.append(command_id)
        self._index.add(command_id)
        while len(self._seen) > self._cap:
            old = self._seen.pop(0)
            self._index.discard(old)
        return True


def _os_name() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "mac"
    if p.startswith("win"):
        return "win"
    return p


def build_argv(
    action: str, payload: dict[str, Any], *, os_name: str | None = None
) -> list[str] | None:
    """Команда процесса для action. None — исполняется не одним процессом (system)."""
    os_name = os_name or _os_name()
    if action == "notify":
        text = str(payload.get("text") or "")
        if os_name == "linux":
            return ["notify-send", "Aegis", text]
        if os_name == "mac":
            script = f'display notification {json_dq(text)} with title "Aegis"'
            return ["osascript", "-e", script]
        if os_name == "win":
            ps = (
                "[System.Reflection.Assembly]::LoadWithPartialName"
                "('System.Windows.Forms') | Out-Null; "
                f"[System.Windows.Forms.MessageBox]::Show({ps_str(text)}, 'Aegis')"
            )
            return ["powershell", "-NoProfile", "-Command", ps]
        raise ValueError(f"notify: не знаю ОС {os_name}")
    if action == "screenshot":
        # путь gettempdir()+имя с суффиксом: прямой /tmp/... — это ещё и symlink-атака на
        # чужом многопользовательском хосте; gettempdir даёт каталог владельца
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(tempfile.gettempdir(), f"aegis-shot-{stamp}.png")
        if os_name == "linux":
            for tool in (
                ["grim", path],
                ["gnome-screenshot", "-f", path],
                ["scrot", path],
            ):
                if shutil.which(tool[0]):
                    return [*tool]
            raise ValueError(
                "screenshot: нет grim/gnome-screenshot/scrot — установите или уберите команду"
            )
        if os_name == "mac":
            return ["screencapture", "-x", path]
        if os_name == "win":
            # только в буфер и файл через powershell — без внешних утилит; не идеально, но честно
            return None  # обрабатывается execute_screenshot_win
        raise ValueError(f"screenshot: не знаю ОС {os_name}")
    if action == "run":
        command = str(payload.get("command") or "")
        if os_name == "win":
            return ["powershell", "-NoProfile", "-Command", command]
        return ["/bin/bash", "-lc", command]
    return None


def json_dq(text: str) -> str:
    """Экранирование для applescript-строки: только кавычки и бэк슬эши (остальное — текст)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ps_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def system_snapshot() -> dict[str, Any]:
    u: dict[str, Any] = {
        "os": f"{platform.system()}-{platform.release()}"[:80],
        "hostname": platform.node()[:64],
        "python": platform.python_version(),
        "uptime_s": None,
        "disk_free_gb": None,
    }
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            u["uptime_s"] = int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        # windows/mac: uptime из subprocess — цена ошибки нулевая, поле просто absent
        pass
    try:
        total, _used, free = shutil.disk_usage(os.path.expanduser("~"))
        u["disk_free_gb"] = round(free / (1024**3), 1)
        u["disk_total_gb"] = round(total / (1024**3), 1)
    except OSError:
        pass
    return u


def execute(payload_cmd: dict[str, Any]) -> dict[str, Any]:
    """Один исполненный шаг → готовый result-конверт для публикации."""
    action = str(payload_cmd.get("action") or "")
    payload = dict(payload_cmd.get("payload") or {})
    out: dict[str, Any] = {"action": action, "ok": True}
    if action == "system":
        out["result"] = payload_dump(system_snapshot())
        return out
    if action == "run":
        argv = build_argv("run", payload)
        assert argv is not None
        timeout = int(payload.get("timeout_s") or 60)
        return _run_proc(argv, timeout=timeout)
    if action == "notify":
        try:
            argv = build_argv("notify", payload)
        except ValueError as exc:
            out["ok"], out["error"] = False, str(exc)[:200]
            return out
        if argv is None:
            out["ok"], out["error"] = False, f"notify не для {_os_name()}"
            return out
        return _run_proc(argv, timeout=15)
    if action == "screenshot":
        return _screenshot()
    out["ok"], out["error"] = False, f"неизвестное действие {action!r}"
    return out


def payload_dump(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, ensure_ascii=False, default=str)[:_MAX_OUTPUT]


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:2000] + "\n…[срезано]…\n" + text[-1500:]


def _run_proc(argv: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(  # noqa: S603 - «исполнить команду владельца на его машине» = смысл фичи
            argv, capture_output=True, timeout=timeout, check=False, shell=False
        )
    except subprocess.TimeoutExpired:
        return {"action": "run", "ok": False, "error": f"таймаут {timeout}s (процесс убит)"}
    except (OSError, ValueError) as exc:
        return {"action": "run", "ok": False, "error": f"запуск не удался: {str(exc)[:200]}"}
    text = (proc.stdout or b"").decode("utf-8", "replace") + (
        "\n[stderr]\n" + (proc.stderr or b"").decode("utf-8", "replace") if proc.stderr else ""
    )
    return {
        "action": "run",
        "ok": proc.returncode == 0,
        "result": _truncate(text or "(без вывода)"),
        "error": None if proc.returncode == 0 else f"exit={proc.returncode}",
    }


_SHOT_RX = re.compile(r"(.*/aegis-shot-[\d-]+\.png)")


def _screenshot() -> dict[str, Any]:
    os_name = _os_name()
    path: str | None = None
    if os_name in ("linux", "mac"):
        try:
            argv = build_argv("screenshot", {"display": "auto"}, os_name=os_name)
        except ValueError as exc:
            return {"action": "screenshot", "ok": False, "error": str(exc)[:200]}
        if argv is None:
            return {"action": "screenshot", "ok": False, "error": "нет известной утилиты скриншота"}
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=20, check=False)  # noqa: S603
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"action": "screenshot", "ok": False, "error": f"{type(exc).__name__}"}
        if proc.returncode != 0:
            return {
                "action": "screenshot",
                "ok": False,
                "error": (proc.stderr or b"").decode("utf-8", "replace")[:200]
                or "скриншот не снят",
            }
        # путь утилиты кладёт в argv строку — достаём её оттуда же (grim/scrot пишут сами)
        for bit in argv:
            if m := _SHOT_RX.fullmatch(bit):
                path = m.group(1)
                break
    else:
        return {
            "action": "screenshot",
            "ok": False,
            "error": "win screenshot: v1 не поддерживается",
        }
    if path is None:
        # gnome-screenshot/scrot приняли путь аргументом — ищем самый свежий файл
        import glob

        candidates = sorted(
            glob.glob(os.path.join(tempfile.gettempdir(), "aegis-shot-*.png")), key=os.path.getmtime
        )
        path = candidates[-1] if candidates else None
    if path is None:
        return {"action": "screenshot", "ok": False, "error": "файл скриншота не найден"}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"action": "screenshot", "ok": False, "error": f"файл не читается: {exc}"[:200]}
    b64 = base64.b64encode(raw).decode("ascii")
    if len(b64) > _MAX_B64:
        return {
            "action": "screenshot",
            "ok": True,
            "result": (
                f"скриншот сохранён на машине: {path}"
                f" ({size // 1024} КБ — слишком велик для брокера, картинка не переслана)"
            ),
        }
    return {"action": "screenshot", "ok": True, "result": f"{path}\n{b64}"}
