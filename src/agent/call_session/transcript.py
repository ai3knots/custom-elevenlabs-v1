"""The call transcript as it is built during the call and stored at the end."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.agent.persona_agent import is_closing_line


class TranscriptBuffer:
    def __init__(self, since_pickup: Callable[[], float]) -> None:
        self._since_pickup = since_pickup
        self.entries: list[dict[str, Any]] = []

    def add_held_turn(self, text: str) -> None:
        """Store what the callee said before the opening line (the LLM does not answer it)."""
        self.entries.append(
            {"role": "user", "message": text, "time_in_call_secs": self._since_pickup(), "interrupted": False}
        )

    def add_item(self, ev: Any) -> bool:
        """Store a finished conversation item. True when it is an agent goodbye that was spoken to the end."""
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "type", None) != "message":
            return False
        role = {"user": "user", "assistant": "agent"}.get(item.role)
        text = item.text_content
        if role is None or not text:
            return False
        interrupted = bool(getattr(item, "interrupted", False))
        self.entries.append(
            {"role": role, "message": text, "time_in_call_secs": self._since_pickup(), "interrupted": interrupted}
        )
        return role == "agent" and not interrupted and is_closing_line(text)
