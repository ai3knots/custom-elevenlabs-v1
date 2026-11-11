"""Mock implementations of STT, LLM, TTS, and VAD for offline testing and verification."""

from __future__ import annotations

import asyncio
from typing import Any

from livekit import rtc
from livekit.agents import llm, stt, tts, vad
from livekit.agents.language import LanguageCode
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions


class MockSTT(stt.STT):
    """Mock Speech-to-Text provider returning deterministic transcriptions."""

    def __init__(self, default_text: str = "Hello from mock STT") -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self.default_text = default_text

    async def _recognize_impl(
        self,
        buffer: Any,
        *,
        language: str | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                stt.SpeechData(
                    language=LanguageCode(language or "en"),
                    text=self.default_text,
                )
            ],
        )


class MockLLMStream(llm.LLMStream):
    """Mock LLM response stream producing a single chat chunk."""

    def __init__(
        self,
        llm_instance: llm.LLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
        response_text: str = "Mock LLM response",
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self.response_text = response_text

    async def _run(self) -> None:
        chunk = llm.ChatChunk(
            id="mock_chunk_0",
            delta=llm.ChoiceDelta(
                content=self.response_text,
                role="assistant",
            ),
        )
        self._event_ch.send_nowait(chunk)


class MockLLM(llm.LLM):
    """Mock LLM provider returning deterministic responses."""

    def __init__(self, default_response: str = "Mock LLM response") -> None:
        super().__init__()
        self.default_response = default_response

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = None,
        tool_choice: Any = None,
        extra_kwargs: Any = None,
    ) -> llm.LLMStream:
        return MockLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            response_text=self.default_response,
        )


class MockChunkedStream(tts.ChunkedStream):
    """Mock TTS stream emitting silent audio frames without external network calls."""

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id="mock_audio_req",
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
        )
        frame = rtc.AudioFrame.create(
            sample_rate=24000,
            num_channels=1,
            samples_per_channel=480,
        )
        output_emitter.push_frame(frame)


class MockTTS(tts.TTS):
    """Mock Text-to-Speech provider producing synthetic audio frames."""

    def __init__(self, sample_rate: int = 24000) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return MockChunkedStream(
            tts=self,
            input_text=text,
            conn_options=conn_options,
        )


class MockVADStream(vad.VADStream):
    """Mock VAD stream for non-blocking testing."""

    async def _main_task(self) -> None:
        # Keep stream active without emitting speech triggers
        await asyncio.sleep(0.01)


class MockVAD(vad.VAD):
    """Mock Voice Activity Detector."""

    def __init__(self) -> None:
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.02))

    def stream(self) -> vad.VADStream:
        return MockVADStream(self)


class ScriptedLLMStream(llm.LLMStream):
    """Replays a fixed list of chunks, so a test can make the model speak and call tools."""

    def __init__(
        self,
        llm_instance: llm.LLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
        chunks: list[llm.ChatChunk],
    ) -> None:
        super().__init__(llm_instance, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._chunks = chunks

    async def _run(self) -> None:
        for chunk in self._chunks:
            self._event_ch.send_nowait(chunk)


class ScriptedLLM(llm.LLM):
    """An LLM whose every reply is the same scripted chunks (text, then optional tool calls)."""

    def __init__(self, text: str = "", tool_calls: list[tuple[str, str]] | None = None) -> None:
        super().__init__()
        self.chunks: list[llm.ChatChunk] = []
        if text:
            self.chunks.append(llm.ChatChunk(id="scripted", delta=llm.ChoiceDelta(role="assistant", content=text)))
        for number, (name, arguments) in enumerate(tool_calls or [], start=1):
            call = llm.FunctionToolCall(name=name, arguments=arguments, call_id=f"call_{number}")
            self.chunks.append(llm.ChatChunk(id="scripted", delta=llm.ChoiceDelta(role="assistant", tool_calls=[call])))

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = None,
        tool_choice: Any = None,
        extra_kwargs: Any = None,
    ) -> llm.LLMStream:
        return ScriptedLLMStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options, chunks=self.chunks
        )
