"""Fakes that let a test run `run_agent_session` end to end without LiveKit, a phone, or any provider.

The real `AgentSession`, `JobContext` and database recorder are replaced by small stand-ins that record what the
call flow does to them. The flow itself (`src/agent/session_handler.py`) runs unmodified.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from livekit import rtc

from src.agent.greeting_detector import GreetingVerdict

CALL_ID = "lk_test0000000001"
PHONE = "+14045551234"
AGENT_ID = "agent_posivex_amelia"


def call_metadata(**overrides: Any) -> str:
    """Dispatch metadata as the API sends it (a `CallRequest` as JSON)."""
    data: dict[str, Any] = {
        "call_id": CALL_ID,
        "phone": PHONE,
        "agent_id": AGENT_ID,
        "persona": "amelia",
        "language": "en",
        "lead_id": "lead-1",
        "campaign_id": "camp-1",
        "dynamic_variables": {"business_name": "Apex Pizza", "contact_name": "Sam"},
    }
    data.update(overrides)
    return json.dumps(data)


def sip_callee(status: str | None = "active", identity: str = f"phone_{PHONE}") -> Any:
    return SimpleNamespace(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        identity=identity,
        attributes={"sip.callStatus": status} if status else {},
    )


def browser_callee() -> Any:
    return SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, identity="tester", attributes={})


async def settle(rounds: int = 20) -> None:
    """Let queued callbacks and tasks run."""
    for _ in range(rounds):
        await asyncio.sleep(0)


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class FakeSpeechHandle:
    """What `session.say()` and `session.generate_reply()` return. Playout ends when the gate opens."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self._gate = gate

    async def wait_for_playout(self) -> None:
        if self._gate is not None:
            await self._gate.wait()
        await asyncio.sleep(0)


class FakeSession:
    """Stand-in for `AgentSession`: records calls, and lets the test fire its events."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.handlers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self.user_state = "listening"
        self.agent_state = "listening"
        self.said: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[dict[str, Any]] = []
        self.interrupts: list[bool] = []
        self.shutdowns: list[bool] = []
        self.away_resets = 0
        self.participants: list[str] = []
        self.agent: Any = None
        self.room_options: Any = None
        self.usage = SimpleNamespace(model_usage=[])
        self.room_io = SimpleNamespace(set_participant=self.participants.append)
        self.playout_gate: asyncio.Event | None = None

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.handlers[event].append(callback)

    def emit(self, event: str, ev: Any = None) -> None:
        for callback in list(self.handlers[event]):
            callback(ev)

    async def start(self, *, agent: Any, room: Any, room_options: Any = None) -> None:
        self.agent = agent
        self.room_options = room_options

    def say(self, text: str, **kwargs: Any) -> FakeSpeechHandle:
        self.said.append((text, kwargs))
        return FakeSpeechHandle(self.playout_gate)

    def generate_reply(self, **kwargs: Any) -> FakeSpeechHandle:
        self.replies.append(kwargs)
        return FakeSpeechHandle()

    def interrupt(self, force: bool = False) -> None:
        self.interrupts.append(force)

    def shutdown(self, drain: bool = True) -> None:
        self.shutdowns.append(drain)
        asyncio.get_running_loop().call_soon(self.emit, "close", SimpleNamespace(reason=None))

    def reset_away_timer(self) -> None:
        self.away_resets += 1

    async def commit_user_turn(self) -> None:
        return None

    # ---- helpers for tests ----------------------------------------------------------------------
    def hear(self, text: str, *, final: bool = True) -> None:
        self.emit("user_input_transcribed", SimpleNamespace(transcript=text, is_final=final))

    def item(self, role: str, text: str, *, interrupted: bool = False) -> None:
        """A finished conversation item: role is 'assistant' or 'user'."""
        self.emit(
            "conversation_item_added",
            SimpleNamespace(item=SimpleNamespace(type="message", role=role, text_content=text, interrupted=interrupted)),
        )

    def user_state_changed(self, new_state: str) -> None:
        self.emit("user_state_changed", SimpleNamespace(new_state=new_state, old_state="listening"))


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_data(self, payload: bytes, *, reliable: bool = True, topic: str = "") -> None:
        self.published.append({"topic": topic, **json.loads(payload.decode())})


class FakeJobContext:
    """Stand-in for LiveKit's `JobContext`. `shutdown()` runs the registered callbacks, like the real one."""

    def __init__(
        self, metadata: str | None, callee: Any = None, room_name: str = f"call_{CALL_ID}", never_joins: bool = False
    ) -> None:
        self.job = SimpleNamespace(metadata=metadata, room=SimpleNamespace(metadata=None))
        self.local = FakeLocalParticipant()
        self.room = SimpleNamespace(name=room_name, metadata=None, remote_participants={}, local_participant=self.local)
        self.proc = SimpleNamespace(userdata={})
        self.api = None
        self.callee = callee
        self.never_joins = never_joins
        self.shutdown_reasons: list[str] = []
        self.shutdown_finished = asyncio.Event()
        self._callbacks: list[Callable[[], Any]] = []
        self._shutdown_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        return None

    async def wait_for_participant(self, **_: Any) -> Any:
        if self.never_joins:
            await asyncio.Event().wait()  # the real call waits for ever
        if self.callee is None:
            raise RuntimeError("no participant joined")
        self.room.remote_participants[self.callee.identity] = self.callee
        return self.callee

    def add_shutdown_callback(self, callback: Callable[[], Any]) -> None:
        self._callbacks.append(callback)

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_reasons.append(reason)
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.get_running_loop().create_task(self._run_callbacks())

    async def _run_callbacks(self) -> None:
        for callback in self._callbacks:
            await callback()
        self.shutdown_finished.set()


class FakeRecorder:
    """Collects what the flow would have written to the database (same surface as `CallRecorder`)."""

    def __init__(self) -> None:
        self.call: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.transcript: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any]] = []
        self.recording: dict[str, Any] | None = None
        self.finalized: dict[str, Any] | None = None

    async def ensure_call(self, **kwargs: Any) -> None:
        self.call = kwargs

    async def record_event(self, call_id: str, event_type: str, state: Any = None, payload: Any = None, **_: Any) -> None:
        self.events.append((event_type, dict(payload or {})))

    async def record_transcript_batch(self, call_id: str, entries: list[dict[str, Any]]) -> int:
        self.transcript = list(entries)
        return len(entries)

    async def record_usage_batch(self, call_id: str, usage: list[dict[str, Any]]) -> list[Any]:
        self.usage = list(usage)
        return []

    async def record_recording(self, call_id: str, info: dict[str, Any]) -> None:
        self.recording = info

    async def finalize_call(self, call_id: str, **kwargs: Any) -> None:
        self.finalized = {"call_id": call_id, **kwargs}

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]

    def payload(self, name: str) -> dict[str, Any]:
        return next(p for n, p in self.events if n == name)


class FakeClassifier:
    """Replaces `GeminiGreetingClassifier`: answers with whatever verdict the test queued (default: human)."""

    verdict = GreetingVerdict("human", 0.95, 0.1)
    asked: list[str] = []

    def __init__(self, api_key: str, model: str, timeout: float = 4.0) -> None:
        self.api_key, self.model = api_key, model

    async def warm(self) -> None:
        return None

    async def classify(self, transcript: str) -> GreetingVerdict:
        FakeClassifier.asked.append(transcript)
        return FakeClassifier.verdict
