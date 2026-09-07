"""Хаб автоматизации: исходящие действия (endpoints + журнал) и входящие вебхуки.

Принцип, который отличает это от curl: секреты владельца запечатаны ключевой цепью
(platform/vault) и живут только в момент вызова; журнал run пишется без секретов; исходящий
URL проходит сетевой guard (SSRF); входящий вебхук не имеет прав ничего менять — максимум,
что он может, — прислать текст владельцу («notify») или инициировать агентный ход с телом
в untrusted-контейнере («turn»). Решения по-прежнему за владельцем.
"""

from aegis.automation.execute import (
    ActionError,
    ActionResult,
    mask_secrets,
    render_template,
    run_endpoint,
)
from aegis.automation.hooks_http import HookOutcome, handle_hook_request, serve_hooks
from aegis.automation.store import EndpointRow, HookRow, SqlAutomationStore

__all__ = [
    "ActionError",
    "ActionResult",
    "EndpointRow",
    "HookOutcome",
    "HookRow",
    "SqlAutomationStore",
    "handle_hook_request",
    "mask_secrets",
    "render_template",
    "run_endpoint",
    "serve_hooks",
]
