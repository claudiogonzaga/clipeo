"""geminiTTS.py — síntese de voz da ANÁLISE via Gemini TTS (para ouvir e para
salvar a nota de áudio no vault). Paridade com o Aspis Android.

O modelo `gemini-2.5-flash-preview-tts` devolve PCM 16-bit mono a 24 kHz; aqui
embrulhamos num WAV. Reutiliza a mesma chave do Gemini (env GEMINI_API_KEY).
"""
import io
import os
import re
import wave

TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Aoede"
SAMPLE_RATE = 24000


def resolve_key(cfg):
    """Chave do Gemini a partir do env configurado (mesma lógica do brain.py)."""
    env = "GEMINI_API_KEY"
    try:
        env = cfg["llm"]["providers"]["gemini"].get("api_key_env", env)
    except (KeyError, TypeError):
        pass
    key = os.environ.get(env)
    if not key:
        raise RuntimeError(f"Chave do Gemini não configurada (defina {env}).")
    return key


def narration(v):
    """Texto falado da análise: resumo + pontos-chave (o que o desktop tem)."""
    parts = []
    if v.get("resumo"):
        parts.append("Resumo: " + v["resumo"])
    pontos = v.get("pontos_chave") or []
    if pontos:
        parts.append("Pontos-chave: " + ". ".join(p.rstrip(".") for p in pontos))
    text = ". ".join(parts).strip()
    return text or v.get("resumo") or v.get("neutral_title") or ""


def _pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _chunks(text, max_len=600):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return [text] if text else []
    sentences = re.findall(r"[^.!?]+[.!?]*\s*", text) or [text]
    out, cur = [], ""
    for s in sentences:
        if len(cur) + len(s) > max_len and cur:
            out.append(cur.strip())
            cur = s
        else:
            cur += s
    if cur.strip():
        out.append(cur.strip())
    return out


def synthesize(text, api_key, voice=DEFAULT_VOICE) -> bytes:
    """Sintetiza `text` na voz `voice` e devolve os bytes de um WAV."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    speech = types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
        )
    )
    pcm = bytearray()
    for chunk in _chunks(text) or [text]:
        resp = client.models.generate_content(
            model=TTS_MODEL,
            contents=chunk,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=speech,
            ),
        )
        data = resp.candidates[0].content.parts[0].inline_data.data
        pcm.extend(data)
    if not pcm:
        raise RuntimeError("Gemini TTS: resposta sem áudio.")
    return _pcm_to_wav(bytes(pcm))
