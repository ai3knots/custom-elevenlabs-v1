"""SQLAlchemy 2 async models for call state, events, transcripts, costs, and webhook outbox."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


def utcnow() -> datetime:
    """Current UTC datetime helper."""
    return datetime.now(UTC)


class CallStatus(enum.StrEnum):
    """Lifecycle status of a phone call."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CallOutcome(enum.StrEnum):
    """The 14 strict call outcomes matching Posivex-lead-gen classification contract."""

    CONSULTANT_CALL_SCHEDULED = "consultant_call_scheduled"
    EMAIL_COMPARISON = "email_comparison"
    INFO_EMAIL = "info_email"
    OWNER_DETAILS_CAPTURED = "owner_details_captured"
    CALLBACK_SET = "callback_set"
    DECLINED = "declined"
    DO_NOT_CALL = "do_not_call"
    EMPLOYEE_NO_OWNER_DETAILS = "employee_no_owner_details"
    VOICEMAIL = "voicemail"
    AUTOMATED_MENU = "automated_menu"
    NO_ANSWER = "no_answer"
    HANGUP_NO_SPEAK = "hangup_no_speak"
    UNKNOWN_DROPPED = "unknown_dropped"
    OTHER = "other"


class WebhookStatus(enum.StrEnum):
    """Transactional outbox delivery status."""

    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class Call(Base):
    """Primary record for an outbound or inbound phone conversation."""

    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lead_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    persona: Mapped[str] = mapped_column(String(32), default="amelia")
    language: Mapped[str] = mapped_column(String(8), default="en")

    status: Mapped[str] = mapped_column(
        String(32), default=CallStatus.QUEUED.value, nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_secs: Mapped[float | None] = mapped_column(Float, nullable=True)

    termination_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    outcome_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_successful: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transcript_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    charging_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # Relationships
    events: Mapped[list[CallEvent]] = relationship(
        "CallEvent", back_populates="call", cascade="all, delete-orphan"
    )
    transcripts: Mapped[list[Transcript]] = relationship(
        "Transcript", back_populates="call", cascade="all, delete-orphan"
    )
    usage_costs: Mapped[list[UsageCost]] = relationship(
        "UsageCost", back_populates="call", cascade="all, delete-orphan"
    )
    webhook_outbox: Mapped[list[WebhookOutbox]] = relationship(
        "WebhookOutbox", back_populates="call", cascade="all, delete-orphan"
    )


class CallEvent(Base):
    """Discrete state transition or system event during a call."""

    __tablename__ = "call_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    call: Mapped[Call] = relationship("Call", back_populates="events")


class Transcript(Base):
    """Individual spoken utterance entry in the conversation history."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # agent | user | system
    message: Mapped[str] = mapped_column(Text, nullable=False)
    time_in_call_secs: Mapped[float] = mapped_column(Float, default=0.0)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship("Call", back_populates="transcripts")


class UsageCost(Base):
    """Provider-level token, character, or audio duration cost ledger."""

    __tablename__ = "usage_costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # elevenlabs | google | deepgram | livekit
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # characters | tokens_input | tokens_output | audio_seconds
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship("Call", back_populates="usage_costs")


class WebhookOutbox(Base):
    """Transactional outbox queue for resilient webhook delivery to Posivex-lead-gen."""

    __tablename__ = "webhook_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(
        String(64), default="post_call_transcription", nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    destination_url: Mapped[str] = mapped_column(String(512), nullable=False)
    signature_header: Mapped[str | None] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(
        String(32), default=WebhookStatus.PENDING.value, nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=10)
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped[Call] = relationship("Call", back_populates="webhook_outbox")


class AgentConfigVersion(Base):
    """Immutable version snapshot of agent configurations for audit and rollback."""

    __tablename__ = "agent_config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    persona: Mapped[str] = mapped_column(String(32), nullable=False)
    config_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Experiment(Base):
    """A/B experiment assignment and performance metric tracking."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    call_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Create multi-column indexes for fast queries
Index("idx_calls_lead_status", Call.lead_id, Call.status)
Index("idx_outbox_status_retry", WebhookOutbox.status, WebhookOutbox.next_retry_at)
