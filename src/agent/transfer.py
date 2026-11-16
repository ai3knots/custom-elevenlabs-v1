"""Live transfer of a call to our team's phone: a cold transfer, a SIP REFER through the call's own trunk.

When the owner asks to talk to a person, the agent calls the `transfer_call` tool. The tool says one scripted line, tells
lead-gen's team-briefing webhook who is about to arrive (so the team has context while the line rings), and asks LiveKit to
transfer the caller to the team line (`TransferSIPParticipant`). When the carrier reports that the team line answered, LiveKit
ends its side of the call and the caller stays connected to the team through the carrier. When it does not connect (no
answer, rejected), the caller is still in the room and the agent carries on: it offers the team line or a callback.

The REFER runs in its own task. A successful transfer makes LiveKit close the caller's session, which can cancel the tool
before it returns, so the bookkeeping (the call is marked transferred, the event is written) lives in the task, and the
finalizer waits for it (`settle`) before it reports the call.

Reference: https://docs.livekit.io/telephony/features/transfers/cold/
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from livekit import api
from livekit.agents import RunContext
from livekit.agents.llm import ToolError, function_tool

from src.config import CallRequest, TransferConfig

if TYPE_CHECKING:
    from src.agent.persona_agent import CallState

logger = logging.getLogger("posivex.session")

HANDOVER_LINE = "Of course. Let me connect you with someone from our team right now."
# Said instead when the owner said yes to the demo now (the live ElevenLabs agent's line for that).
DEMO_HANDOVER_LINE = "Perfect, let me connect you with our team now. They'll show you the devices and features live."
# While the team line rings the call is on hold: the silence timers must not end it.
HOLD_MARGIN_SECS = 15.0
MAX_BRIEF_CHARS = 3000

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]
EndCall = Callable[[str, bool], None]


@dataclass(frozen=True)
class TransferBrief:
    """What the agent tells our team about the person who is about to be connected."""

    summary: str = ""
    prospect_name: str = ""
    prospect_email: str = ""
    callback_number: str = ""
    demo: bool = False  # connected because they want the demo now, not only to talk to a person


class TransferFailed(Exception):
    """The transfer did not connect. `kind` is one of: no_answer, rejected, call_ended, failed."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


# What the model is told when a transfer did not connect (it then says the right thing to the owner).
FAILURE_MESSAGES = {
    "no_answer": "Nobody on our team line answered.",
    "rejected": "Our team line could not take the call (it was busy or declined it).",
    "call_ended": "The call ended during the transfer.",
    "failed": "The transfer could not be completed.",
}
FAILURE_NEXT_STEP = (
    " Tell the owner you could not reach the team right now, then give our team line or offer to have someone call them back "
    "and take a day and time. Carry on with the call."
)


def classify_failure(error: BaseException) -> TransferFailed:
    """LiveKit's error (or a timeout) as a TransferFailed with a plain kind. `sip_transfer_reason` is documented here:
    https://docs.livekit.io/telephony/features/transfers/cold/#handle-transfer-failures"""
    if isinstance(error, TimeoutError):
        return TransferFailed("no_answer", "no answer from LiveKit before the timeout")
    if isinstance(error, api.SipCallError):
        reason = error.sip_transfer_reason or ""
        status = f"{error.sip_status_code} {error.sip_status or ''}".strip() if error.sip_status_code else ""
        if "RINGING_TIMEOUT" in reason:
            return TransferFailed("no_answer", reason)
        if "CALL_ENDED" in reason:
            return TransferFailed("call_ended", reason)
        if "REJECTED" in reason or status:
            return TransferFailed("rejected", " ".join(p for p in (reason, status) if p))
        return TransferFailed("failed", reason or str(error))
    code = getattr(error, "code", "")
    if code == "canceled":  # HTTP 408 with no transfer reason: the ringing timeout ran out
        return TransferFailed("no_answer", "canceled (408)")
    return TransferFailed("failed", f"{type(error).__name__}: {error}")


class CallTransferrer:
    def __init__(
        self,
        *,
        ctx: Any,
        call_request: CallRequest,
        config: TransferConfig,
        call_state: CallState,
        is_phone_call: Callable[[], bool],
        callee_identity: Callable[[], str | None],
        call_ending: Callable[[], bool],
        end_call: EndCall,
        emit: EventEmitter,
    ) -> None:
        self._ctx = ctx
        self._request = call_request
        self._config = config
        self._state = call_state
        self._is_phone_call = is_phone_call
        self._callee_identity = callee_identity
        self._call_ending = call_ending
        self._end_call = end_call
        self._emit = emit
        self._attempts = 0
        self._task: asyncio.Task[None] | None = None

    # ---- the tool ------------------------------------------------------------------------------
    async def transfer(self, context: RunContext, brief: TransferBrief) -> None:
        """Transfer the call. Returns when it connected (the call is ending); raises ToolError when it did not."""
        self._check_allowed()
        self._attempts += 1
        state = self._state
        state.transferring = True
        state.start_hold(self._config.ringing_timeout_seconds + HOLD_MARGIN_SECS)
        task = asyncio.ensure_future(self._attempt(context.session, brief))
        # If the tool is cancelled (the caller's session closed) nobody awaits the task: still collect its exception.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._task = task
        try:
            await asyncio.shield(task)
        except TransferFailed as failed:
            message = FAILURE_MESSAGES.get(failed.kind, FAILURE_MESSAGES["failed"])
            raise ToolError(message + FAILURE_NEXT_STEP) from None

    def _check_allowed(self) -> None:
        state, config = self._state, self._config
        if not self._is_phone_call():
            raise ToolError("This is not a phone call that can be transferred.")
        if state.voicemail_detected or state.greeting_verdict in ("voicemail", "menu"):
            raise ToolError("A machine answered, not a person. Do not transfer; use voicemail_detection.")
        if self._call_ending():
            raise ToolError("The call is already ending.")
        if state.transferred or state.transferring:
            raise ToolError("A transfer is already in progress or done. Do not start another one.")
        if self._attempts >= config.max_attempts:
            raise ToolError(
                f"The transfer was already tried {self._attempts} times and did not connect. Do not try again: "
                "give our team line or offer a callback."
            )
        if not self._callee_identity():
            raise ToolError("The caller cannot be found in the room, so the call cannot be transferred.")

    # ---- the attempt (its own task, see the module docstring) ----------------------------------
    async def _attempt(self, session: Any, brief: TransferBrief) -> None:
        state = self._state
        briefing = asyncio.ensure_future(self._send_briefing(brief))
        try:
            handle = session.say(DEMO_HANDOVER_LINE if brief.demo else HANDOVER_LINE, allow_interruptions=False)
            await handle.wait_for_playout()
            await self._refer()
        except Exception as error:
            failed = error if isinstance(error, TransferFailed) else classify_failure(error)
            state.transferring = False
            state.release_hold()
            logger.warning("Transfer to the team line did not connect (%s: %s).", failed.kind, failed.detail)
            await self._emit(
                "transfer_failed",
                {
                    "reason": failed.kind,
                    "detail": failed.detail,
                    "result": f"Transfer failed: {FAILURE_MESSAGES.get(failed.kind, failed.kind)}",
                    "error": True,
                    "attempt": self._attempts,
                    "demo": brief.demo,
                },
            )
            await briefing
            raise failed from None
        state.transferred = True
        state.transferring = False
        state.end_reason = "transferred"
        logger.info("Call transferred to the team line.")
        await self._emit(
            "call_transferred",
            {"reason": "transferred", "result": "Call was transferred to number", "attempt": self._attempts, "demo": brief.demo},
        )
        await briefing
        self._end_call("transferred", False)

    async def _refer(self) -> None:
        identity = self._callee_identity()
        request = api.TransferSIPParticipantRequest(
            room_name=self._ctx.room.name,
            participant_identity=identity or "",
            transfer_to=f"tel:{self._config.destination}",
            play_dialtone=self._config.play_dialtone,
        )
        request.ringing_timeout.FromSeconds(int(self._config.ringing_timeout_seconds))
        logger.info("Transferring the call to the team line.")
        # LiveKit stops waiting at the ringing timeout; this is only a backstop for a request that never answers.
        await asyncio.wait_for(
            self._ctx.api.sip.transfer_sip_participant(request), timeout=self._config.ringing_timeout_seconds + 10.0
        )

    async def settle(self) -> None:
        """Wait for a transfer in flight to finish, so the call is reported as transferred or not. Never raises."""
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), self._config.ringing_timeout_seconds + 15.0)
        except Exception:
            logger.debug("The transfer did not finish cleanly before the report.", exc_info=True)

    # ---- the team briefing ---------------------------------------------------------------------
    async def _send_briefing(self, brief: TransferBrief) -> None:
        """Tell lead-gen's team who is being connected. Time-boxed and best effort: it must never hold up or fail a transfer."""
        config = self._config
        if not config.briefing_url or not config.briefing_secret:
            logger.info("No team briefing configured (TEAM_BRIEFING_URL, TEAM_BRIEFING_SECRET); the transfer goes ahead without it.")
            return
        variables = self._request.dynamic_variables
        body = {
            "conversation_id": self._request.call_id or "",
            "intent": "transfer_now",
            "business_name": variables.business_name,
            "business_context": variables.business_context,
            "current_datetime": variables.current_datetime,
            "prospect_phone": self._request.phone,
            "prospect_name": brief.prospect_name or variables.contact_name,
            "prospect_email": brief.prospect_email,
            "callback_number": brief.callback_number,
            "scheduled_time": "",
            "summary": brief.summary[:MAX_BRIEF_CHARS],
        }
        try:
            async with httpx.AsyncClient(timeout=config.briefing_timeout_seconds) as client:
                response = await client.post(config.briefing_url, json=body, headers={"X-Agent-Tool-Secret": config.briefing_secret})
            if response.status_code >= 400:
                logger.warning("The team briefing was refused (HTTP %s).", response.status_code)
            else:
                logger.info("Team briefing sent.")
        except Exception as error:
            logger.warning("The team briefing could not be sent (%s).", type(error).__name__)


def build_transfer_tool(transferrer: CallTransferrer) -> Any:
    """The `transfer_call` tool, built only for a call that may be transferred (so the model never sees it otherwise)."""

    async def transfer_call(
        context: RunContext,
        summary: str = "",
        prospect_name: str = "",
        prospect_email: str = "",
        callback_number: str = "",
        demo: bool = False,
    ) -> None:
        """Connect the caller to our team on a live phone line. Use it only when the owner or the person who makes the decision asks to talk to a person or wants the demo right now, or when someone says yes to your offer in the AI answer. Do not speak before it: the tool says the hand-over line itself. Never use it for a machine or a menu, and never to offer a transfer on your own apart from that AI answer. It also emails our sales team a briefing about this prospect while the call is being connected; never mention that to the prospect. If it fails you are told why and the call goes on.

        Args:
            summary: Three to six plain sentences for the sales team, using only what was said on this call: what the prospect uses today for card payments, what they said or asked (prices, objections, interest), what they agreed to, and anything the team should know before talking to them. Never invent details.
            prospect_name: The prospect's name if they said it, otherwise empty
            prospect_email: Their email if they gave it, otherwise empty
            callback_number: A number to reach them if they gave one, otherwise empty
            demo: True when they are being connected because they said yes to the demo now or asked for it right now, otherwise false
        """
        await transferrer.transfer(
            context,
            TransferBrief(
                summary=summary or "",
                prospect_name=prospect_name or "",
                prospect_email=prospect_email or "",
                callback_number=callback_number or "",
                demo=bool(demo),
            ),
        )

    return function_tool(transfer_call)
