import inspect
import sys

print("=" * 60)
print("1. livekit.plugins.google")
print("=" * 60)
try:
    from livekit.plugins import google
    print("google plugin attrs:", [a for a in dir(google) if not a.startswith("_")])
    if hasattr(google, "LLM"):
        print("google.LLM signature:", inspect.signature(google.LLM.__init__))
        print("google.LLM doc:", google.LLM.__doc__)
except Exception as e:
    print("Error google plugin:", e)

print("\n" + "=" * 60)
print("2. FallbackAdapter in livekit.agents.llm")
print("=" * 60)
try:
    from livekit.agents import llm
    fallback_candidates = [a for a in dir(llm) if "fallback" in a.lower()]
    print("Fallback items in llm:", fallback_candidates)
    if hasattr(llm, "FallbackAdapter"):
        print("FallbackAdapter signature:", inspect.signature(llm.FallbackAdapter.__init__))
        print("FallbackAdapter doc:", llm.FallbackAdapter.__doc__)
    else:
        # Check in whole livekit.agents
        import livekit.agents
        all_fb = [a for a in dir(livekit.agents) if "fallback" in a.lower()]
        print("Fallback in livekit.agents:", all_fb)
except Exception as e:
    print("Error FallbackAdapter:", e)

print("\n" + "=" * 60)
print("3. livekit.plugins.elevenlabs source inspection")
print("=" * 60)
try:
    from livekit.plugins import elevenlabs
    import livekit.plugins.elevenlabs.tts as el_tts
    print("elevenlabs.TTS signature:", inspect.signature(elevenlabs.TTS.__init__))
    print("elevenlabs TTS file path:", inspect.getfile(elevenlabs.TTS))
    print("TTSModels in elevenlabs:", getattr(elevenlabs, "TTSModels", None))
except Exception as e:
    print("Error elevenlabs plugin:", e)

print("\n" + "=" * 60)
print("4. AgentSession expressive parameter")
print("=" * 60)
try:
    from livekit.agents import AgentSession
    import livekit.agents.voice.agent_session as ag_sess_mod
    print("ExpressiveOptions:", getattr(ag_sess_mod, "ExpressiveOptions", None))
except Exception as e:
    print("Error AgentSession expressive:", e)

print("\n" + "=" * 60)
print("5. AssemblyAI STT inspection")
print("=" * 60)
try:
    from livekit.plugins import assemblyai
    print("assemblyai attrs:", [a for a in dir(assemblyai) if not a.startswith("_")])
    if hasattr(assemblyai, "STT"):
        print("assemblyai.STT signature:", inspect.signature(assemblyai.STT.__init__))
except Exception as e:
    print("Error assemblyai plugin:", e)

print("\n" + "=" * 60)
print("6. Deepgram languages")
print("=" * 60)
try:
    from livekit.plugins import deepgram
    dg_langs = getattr(deepgram, "DeepgramLanguages", None)
    print("DeepgramLanguages type:", type(dg_langs))
    if dg_langs:
        # Check en, es, hi, ur
        for lang in ["en", "es", "hi", "ur", "en-US", "es-ES", "hi-IN", "ur-PK"]:
            print(f"  {lang} in DeepgramLanguages?", lang in dg_langs.__members__ if hasattr(dg_langs, "__members__") else "N/A")
except Exception as e:
    print("Error deepgram languages:", e)

