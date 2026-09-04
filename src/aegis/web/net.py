"""Сетевые ограничения: SSRF-фильтр для fetch-инструментов.

Агент умеет ходить по URL из внешнего контента (страница, чек, ссылка в письме) — это прямой
путь к ``http://169.254.169.254/latest/meta-data/`` и к внутренним сервисам хоста. Поэтому перед
запросом имя резолвится и проверяется: только публичные адреса, редиректы проверяются заново.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit

from aegis.platform.config import settings

__all__ = ["BlockedTarget", "guard_url", "safe_redirect"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedTarget(ValueError):
    """URL указывает на приватную/запрещённую сеть."""


def _ip_allowed(host: str, *, allow_private: bool) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:  # DNS не разрешился — не лезем вслепую
        raise BlockedTarget(f"не разрешился хост {host!r}: {exc}") from exc
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise BlockedTarget(f"странный адрес {raw!r}") from exc
        if allow_private:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise BlockedTarget(f"адрес {ip} в приватном/служебном диапазоне")
    return True


def guard_url(url: str, *, allow_private: bool | None = None) -> str:
    """Проверить и нормализовать URL. Возвращает канонический https-адрес без fragment."""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise BlockedTarget(f"схема {parts.scheme!r} запрещена")
    host = parts.hostname
    if not host:
        raise BlockedTarget("в URL нет хоста")
    allow = settings().fetch_allow_private if allow_private is None else allow_private
    _ip_allowed(host, allow_private=allow)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def safe_redirect(location: str, base: str) -> str:  # noqa: ARG001 - base пока не нужен, но входит в контракт
    """Редирект проверяется так же строго, как и исходный запрос."""
    return guard_url(location)
