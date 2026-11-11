"""What happens when the call is over: the outcome, the cost, and the report that goes to the API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from livekit.agents.metrics.usage import LLMModelUsage, STTModelUsage, TTSModelUsage
from sqlalchemy.ext.asyncio import AsyncEngine

from src.agent.call_session.lifecycle import CallLifecycle
from src.agent.call_session.timeline import CallTimeline
from src.agent.call_session.transcript import TranscriptBuffer
from src.agent.greeting_detector import machine_outcome
from src.agent.persona_agent import CallState
from src.analysis import CallSignals, OutcomeResult, resolve_call_outcome
from src.metrics.costs import CostAccumulator

logger = logging.getLogger("posivex.session")

# Classifies the transcript into an outcome (the Gemini call). Injected so a test can answer without a network.
OutcomeClassifier = Callable[[Sequence[Mapping[str, Any]], CallSignals], Awaitable[OutcomeResult]]


@dataclass
class PhoneLeg:
    """Whether this is a phone call, and whether the callee ever picked up (set once the callee joins)."""

    sip: bool = False
    answered: bool = False


def usage_into_costs(session: Any, costs: CostAccumulator, duration_secs: float) -> None:
    """Fold LiveKit's per-model session usage into the cost accumulator."""
    try:
        for usage in session.usage.model_usage:
            if isinstance(usage, LLMModelUsage):
                costs.add_llm_usage(
                    usage.model, usage.input_tokens, usage.output_tokens, cached_tokens=usage.input_cached_tokens
                )
                if usage.input_tokens:
                    logger.info(
                        "LLM usage: model=%s input=%d cached=%d (%.0f%%) output=%d",
                        usage.model,
                        usage.input_tokens,
                        usage.input_cached_tokens,
                        usage.input_cached_tokens / usage.input_tokens * 100,
                        usage.output_tokens,
                    )
            elif isinstance(usage, TTSModelUsage):
                costs.add_tts_usage(usage.model, usage.characters_count)
            elif isinstance(usage, STTModelUsage):
                costs.add_stt_usage(usage.model, usage.audio_duration)
    except Exception as e:
        logger.warning("Could not read session usage: %s", e)
    costs.add_telephony_usage(duration_secs)


class CallFinalizer:
    def __init__(
        self,
        *,
        session: Any,
        call_state: CallState,
        lifecycle: CallLifecycle,
        timeline: CallTimeline,
        transcript: TranscriptBuffer,
        signals: CallSignals,
        leg: PhoneLeg,
        engine: AsyncEngine | None,
        webhook_destination: str,
        agent_id: str,
        agent_name: str,
        classify_outcome: OutcomeClassifier,
    ) -> None:
        self._session = session
        self._call_state = call_state
        self._lifecycle = lifecycle
        self._timeline = timeline
        self._transcript = transcript
        self._signals = signals
        self._leg = leg
        self._engine = engine
        self._webhook_destination = webhook_destination
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._classify_outcome = classify_outcome
        self._costs = CostAccumulator()
        self._finalized = False

    async def run(self) -> None:
        """Decide the outcome, store the call and stage the webhook. Does nothing the second time."""
        if self._finalized:
            return
        self._finalized = True
        timeline = self._timeline
        # Counted from pickup, not job start: ringing is not call time (0 for a call nobody answered).
        duration = timeline.clock.since_pickup()
        self._collect_signals()

        result = await resolve_call_outcome(list(self._transcript.entries), self._signals, self._classify_outcome)
        logger.info("Call outcome: %s (%s) [%s]", result.outcome, result.rationale, result.source)
        usage_into_costs(self._session, self._costs, duration)
        reason = self._lifecycle.reason or "normal_completion"
        timeline.mark(
            "call_end",
            reason=reason,
            outcome=result.outcome,
            ring_secs=round(timeline.clock.ring_secs, 2),
            talk_secs=round(duration, 2),
        )
        await timeline.flush()  # store the timing events before the call is finalized
        try:
            await timeline.persist(
                "transcripts", timeline.recorder.record_transcript_batch(timeline.call_id, self._transcript.entries)
            )
            await timeline.persist("usage", timeline.recorder.record_usage_batch(timeline.call_id, self._costs.records))
            await timeline.persist(
                "finalize",
                timeline.recorder.finalize_call(
                    call_id=timeline.call_id,
                    duration_secs=duration,
                    termination_reason=reason,
                    call_outcome=result.outcome,
                    outcome_rationale=result.rationale,
                    transcript_summary=result.summary,
                    total_cost=self._costs.total_cost,
                    charging_details=self._costs.get_charging_breakdown(),
                    destination_url=self._webhook_destination,
                    agent_id=self._agent_id,
                    agent_name=self._agent_name,
                ),
            )
        finally:
            if self._engine is not None:
                await self._engine.dispose()

    def _collect_signals(self) -> None:
        """Facts the transcript cannot show, which decide or inform the outcome."""
        state, signals = self._call_state, self._signals
        signals.greeting_verdict = state.greeting_verdict
        signals.voicemail_tool_called = state.voicemail_detected
        signals.do_not_call_requested = state.do_not_call
        if state.voicemail_detected and not signals.forced_outcome:
            signals.forced_outcome = machine_outcome(state.machine_kind)[0]
            signals.forced_rationale = (
                "The agent detected a phone menu, call screener or system message."
                if signals.forced_outcome == "automated_menu"
                else "The agent detected a voicemail or recorded message."
            )
        if self._leg.sip and not self._leg.answered and not signals.forced_outcome:
            # The phone leg ended while still ringing (declined, busy, network refusal): not a conversation.
            signals.forced_outcome = "no_answer"
            signals.forced_rationale = "The callee did not answer (the call was rejected or dropped while ringing)."
