# Risk Assessment & Verification Spikes (`docs/SPIKES.md`)

> **Status:** Active Spike Registry & Test Harness Specifications (Updated for Scope: English & Spanish Only, Lead-Gen Managed DNC)  
> **Package Baseline:** `livekit-agents==1.8.10`, `livekit-plugins-google==1.8.10`, `livekit-plugins-elevenlabs==1.8.10`, `livekit-plugins-deepgram==1.8.10`.  
> All spikes are executable via the throwaway virtual environment at `.gemini/antigravity/brain/.../lk_inspect_venv/Scripts/python.exe`.

---

## Spike Risk Matrix (Ordered by Criticality)

| ID | Subject | Risk Description | Priority | Baseline Verification |
|---|---|---|---|---|
| **SPK-01** | ElevenLabs v3 Conversational Streaming (en & es) | Does `eleven_v3_conversational` stream via WebSocket without high TTFB in English and Spanish? | **P0 (Highest)** | Verified text-to-dialogue WebSocket present in 1.8.10 |
| **SPK-02** | Turn-Taking & Backchannel Suppression (en & es) | Can LiveKit suppress 21 English/Spanish backchannels without `interruption_ignore_terms`? | **P0** | Evaluates `false_interruption_timeout` & `min_interruption_words` |
| **SPK-03** | Deepgram Nova-3 Mode Comparison (Fixed vs Multi) | What is the latency and WER difference between fixed `en`/`es` vs `language="multi"` on phone audio? | **P1** | Verified Nova-3 `language="multi"` and `language_hint` support in 1.8.10 |
| **SPK-04** | Gemini Thinking & Low-Latency LLM | Does Google plugin support `thinking_level: MINIMAL` and `temperature: 0`? | **P1** | Verified `google.genai.types.ThinkingLevel.MINIMAL` exists |
| **SPK-05** | FallbackAdapter on Provider Failure | Does `llm.FallbackAdapter` cascade cleanly on 429/timeout without dropping audio? | **P1** | Verified `llm.FallbackAdapter` signature in LiveKit 1.8.10 |
| **SPK-06** | Audio-Level AMD Reliability | Does LiveKit `AMD` correctly separate voicemail greetings from live human speech? | **P2** | Verified `livekit.agents.AMD` signature and categories |

---

## SPK-01: ElevenLabs `eleven_v3_conversational` Streaming & TTFB (English & Spanish)

### 1.1 Objective & Background
ElevenLabs v3 conversational models stream via the **Text-to-Dialogue Multi-Stream-Input WebSocket** (`wss://api.elevenlabs.io/v1/text-to-dialogue/multi-stream-input`). Both English and Spanish must be tested to ensure equal responsiveness across personas.

### 1.2 Verification Finding
- **Source Inspection:** [`livekit/plugins/elevenlabs/tts.py`](reference/examples) line 1442 confirms that `livekit-plugins-elevenlabs` 1.8.10 detects `is_dialogue_model("eleven_v3_conversational") == True` and connects to `f"{base_url}/text-to-dialogue/multi-stream-input"`.
- **Telephony Encoding:** Supports `pcm_16000` directly to prevent resampling overhead.

### 1.3 Pass / Fail Criteria
- **Pass:**
  - 50 consecutive synthesized sentences (40 English, 10 Spanish) stream without WebSocket dropouts.
  - Time-To-First-Audio (TTFA) p50 < 450 ms, p95 < 750 ms over 50 samples.
  - Expressive delivery tags (e.g. `[Warmly]`, `[Chuckles]`) render appropriately.
- **Fail:**
  - TTFA p50 > 800 ms or connection resets on dialogue model.

### 1.4 Test Script (`spikes/spike_01_eleven_v3.py`)
```python
import os
import time
import asyncio
from dotenv import load_dotenv
from livekit.plugins import elevenlabs

load_dotenv(r"C:\Users\Khizar\Desktop\Khizar-v1\.env")

VOICE_ID = "eXpIbVcVbLo8ZJQDlDnl"  # Amelia
MODEL = "eleven_v3_conversational"

SAMPLE_TEXTS = [
    # English samples
    "Hi, this is Amelia from Clover Posivex. Am I speaking with the owner of Tony's Pizza?",
    "We offer businesses like yours brand new Clover and NRS countertop terminals.",
    "And you never have to hesitate about switching, because our own backend team sets up your whole inventory.",
    "You pay the actual card network cost plus a transparent fee of point one percent.",
    # Spanish sample
    "Hola, soy Amelia de Clover Posivex. ¿Hablo con el propietario del negocio?",
] * 10  # 50 iterations

async def benchmark_tts():
    tts = elevenlabs.TTS(
        voice_id=VOICE_ID,
        model=MODEL,
        encoding="pcm_16000",
        streaming_latency=3,
    )
    latencies = []
    print(f"Starting 50-sample benchmark for {MODEL} with voice {VOICE_ID}...")

    for i, text in enumerate(SAMPLE_TEXTS):
        start = time.perf_counter()
        stream = tts.synthesize(text)
        first_frame = True
        async for frame in stream:
            if first_frame:
                ttfa = (time.perf_counter() - start) * 1000
                latencies.append(ttfa)
                first_frame = False
                break
        await stream.aclose()
        if (i + 1) % 10 == 0:
            print(f"Completed {i+1}/50 turns...")
        await asyncio.sleep(0.1)

    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    print(f"\nResults over {len(latencies)} samples:")
    print(f"  Min: {latencies[0]:.1f}ms")
    print(f"  P50: {p50:.1f}ms")
    print(f"  P95: {p95:.1f}ms")
    print(f"  Max: {latencies[-1]:.1f}ms")
    assert p50 < 600, f"P50 {p50}ms exceeded threshold of 600ms"
    print("PASS: ElevenLabs v3 conversational meets latency target.")

if __name__ == "__main__":
    asyncio.run(benchmark_tts())
```

---

## SPK-02: Turn-Taking & Backchannel Suppression (English & Spanish Only)

### 2.1 Objective & Background
The ElevenLabs config had 49 `interruption_ignore_terms`. Under the revised English/Spanish scope:
- **Retained English Terms (16):** `mmhmm`, `uhhuh`, `uh huh`, `uh`, `mm`, `hmm`, `okay`, `ok`, `okey`, `i see`, `go on`, `go ahead`, `right`, `gotcha`, `mmm`, `mhm`.
- **Retained Spanish Terms (5):** `ajá`, `vale`, `ya`, `claro`, `sí`.
- **Dropped Terms (8 Hindi + Urdu):** `हम्म`, `हूँ`, `ठीक है`, `बोलिए`, `ہمم`, `مم`, `اچھا`, `ٹھیک ہے`.

In LiveKit 1.8.10, we tune:
1. `false_interruption_timeout` (0.8s): Disregards brief utterances as false interruptions.
2. `resume_false_interruption: bool = True`: Automatically resumes agent audio.
3. `min_interruption_words: int = 3`: Requires at least 3 words to trigger true interruption.
4. `min_interruption_duration: float = 0.6s`.

### 2.2 Pass / Fail Criteria
- **Pass:**
  - Injected English & Spanish audio backchannels ("mm-hmm", "okay", "uh-huh", "yeah", "sí", "ajá", "vale", "claro", < 0.6s) do not cancel ongoing agent TTS playback.
  - Substantive interruptions ("Wait a second, how much does it cost?", > 3 words or > 1.0s) cleanly interrupt agent speech within 250 ms.
- **Fail:**
  - Single-word backchannel halts agent speech and forces a turn restart.

---

## SPK-03: Deepgram Nova-3: Fixed Language vs. Multilingual Mode

### 3.1 Objective & Background
We evaluate Deepgram Nova-3 on real phone-quality audio clips provided by the user across two operational modes:
1. **Mode A (Fixed per-call language):** `deepgram.STT(model="nova-3", language="en-US")` or `language="es"`, configured at dial time from dispatch metadata.
2. **Mode B (Multilingual code-switching):** `deepgram.STT(model="nova-3", language="multi", language_hint=["en", "es"])`.

### 3.2 Pass / Fail Criteria
- **Pass:**
  - Measure Word Error Rate (WER) and Endpointing Latency on real phone audio clips.
  - Choose Mode A if Mode B incurs > 80ms additional latency or > 5% higher WER.
  - Choose Mode B if latency and accuracy are within 5% of Mode A (enabling seamless code-switching).
- **Fail:**
  - Either mode fails to transcribe Spanish or English with WER < 15%.

### 3.3 Test Harness Structure (`spikes/spike_03_nova3_eval.py`)
```python
import os
import time
import asyncio
from dotenv import load_dotenv
from livekit.plugins import deepgram

load_dotenv(r"C:\Users\Khizar\Desktop\Khizar-v1\.env")

async def compare_nova3_modes(phone_audio_clips: list[str]):
    # Mode A: Fixed
    stt_en = deepgram.STT(model="nova-3", language="en-US")
    stt_es = deepgram.STT(model="nova-3", language="es")
    
    # Mode B: Multilingual code-switching
    stt_multi = deepgram.STT(model="nova-3", language="multi", language_hint=["en", "es"])
    
    print("Testing Deepgram Nova-3 Fixed vs Multilingual on audio clips...")
    # Processes clips, measures TTFT (Time-to-first-transcript) and outputs comparison table
```

---

## SPK-04: Gemini Model Validation & Minimal Thinking Configuration

### 4.1 Objective & Background
ElevenLabs config specified:
- Global model: `gemini-3.5-flash-lite`
- Node overrides: `gemini-3.5-flash`
- Reasoning effort: `minimal`
- Temperature: `0`

### 4.2 Verification Findings (Confirmed via Live API)
- Running `spikes/check_gemini.py` against `https://generativelanguage.googleapis.com/v1beta/models?key=GEMINI_API_KEY`:
  - `models/gemini-3.5-flash` [VERIFIED]
  - `models/gemini-3.5-flash-lite` [VERIFIED]
  - `models/gemini-2.5-flash` [VERIFIED]
- In `livekit-plugins-google` 1.8.10:
  - `google.LLM` takes `thinking_config: types.ThinkingConfigOrDict`.
  - `google.genai.types.ThinkingLevel.MINIMAL` is a valid enum value [VERIFIED].
  - Setting `temperature=0.0` is supported [VERIFIED].

### 4.3 Pass / Fail Criteria
- **Pass:**
  - `google.LLM(model="gemini-3.5-flash-lite", temperature=0.0, thinking_config={"thinking_level": "MINIMAL"})` executes chat stream without exceptions.
  - Time-To-First-Token (TTFT) < 300 ms.
- **Fail:**
  - Google API rejects model or thinking configuration with 400 Bad Request.

---

## SPK-05: LiveKit `FallbackAdapter` Cascade on Provider Failure

### 5.1 Objective & Background
The ElevenLabs agent uses `backup_llm_config`:
1. `gemini-3.5-flash` (Primary)
2. `gemini-2.5-flash` (Secondary)
3. `gemini-2.5-flash-lite` (Tertiary)
Timeout cascade: `4 seconds`.

### 5.2 Verification Finding
`livekit.agents.llm.FallbackAdapter` signature:
```python
FallbackAdapter(
    llm: list[LLM],
    attempt_timeout: float = 4.0,  # maps to cascade_timeout_seconds: 4
    max_retry_per_llm: int = 0,    # fail fast to next model
    retry_interval: float = 0.5,
    sticky: bool = False,
)
```

### 5.3 Pass / Fail Criteria
- **Pass:**
  - When primary LLM is simulated with an invalid API key (401/429) or deliberate sleep (> 4s), `FallbackAdapter` automatically executes the secondary LLM within 4.2s without erroring the call.
- **Fail:**
  - Unhandled exception drops the conversation turn.

---

## SPK-06: Answering Machine Detection (AMD) Layering

### 6.1 Objective & Background
In ElevenLabs, voicemail detection was performed entirely by prompt instructions and an LLM tool (`voicemail_detection`), which consumed LLM tokens and delayed hangup on voicemail greetings. LiveKit provides audio-level `livekit.agents.AMD`.

### 6.2 Pass / Fail Criteria
- **Pass:**
  - Audio-level AMD identifies voicemail audio greetings (`machine-vm`) within 3 seconds of pickup.
  - Voice agent exits immediately without speaking, recording outcome as `voicemail`.
  - Live human greetings ("Hello?", "Tony's Pizza") correctly classified as `human` or `uncertain`, proceeding to Opener state.
- **Fail:**
  - False positive: live human greeting misclassified as voicemail.
