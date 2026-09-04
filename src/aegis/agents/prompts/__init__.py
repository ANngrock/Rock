"""Промпты. Версионированы: текст правила меняется — меняется ``PROMPT_VERSION``."""

from __future__ import annotations

from aegis.agents.prompts.system import PROMPT_VERSION, build_system_prompt

__all__ = ["PROMPT_VERSION", "build_system_prompt"]
