#!/usr/bin/env python3
"""
vad_recorder.py — Grabación con VAD por energía RMS + sounddevice

Escucha el micrófono en tiempo real, detecta voz humana por nivel de energía
y envía el audio al servidor cuando hay 2 segundos de silencio consecutivo.

Instalación:
    pip install sounddevice requests python-dotenv

Nota: OPENAI_API_KEY y ANTHROPIC_API_KEY son usadas por el servidor,
      no por este script. Se cargan desde .env por coherencia con el proyecto.
"""

import io
import os
import queue
import struct
import sys
import threading
import wave

import requests
import sounddevice as sd
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────

API_URL          = "https://kiosco-ai.onrender.com/audio"
SAMPLE_RATE      = 16_000    # Hz
FRAME_MS         = 30        # duración de frame en ms
FRAME_SAMPLES    = int(SAMPLE_RATE * FRAME_MS / 1000)   # 480 muestras
RMS_THRESHOLD    = 0.01      # 0.0–1.0 (sube si hay ruido de fondo)
SILENCE_SECS     = 2.0       # segundos de silencio para cerrar bloque
MIN_VOICE_SECS   = 0.5       # mínimo de voz para no descartar

SILENCE_FRAMES   = int(SILENCE_SECS * 1000 / FRAME_MS)
MIN_VOICE_FRAMES = int(MIN_VOICE_SECS * 1000 / FRAME_MS)

# ── Helpers ───────────────────────────────────────────────────────────────────

def frame_rms(frame_bytes: bytes) -> float:
    """RMS normalizado [0.0, 1.0] de un frame PCM 16-bit mono."""
    n = len(frame_bytes) // 2
    samples = struct.unpack(f"{n}h", frame_bytes)
    rms = (sum(s * s for s in samples) / n) ** 0.5
    return rms / 32768.0


def frames_to_wav(frames: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit PCM
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def send_audio(wav_bytes: bytes, voz_secs: float) -> None:
    print(f"\n📤 Enviando {voz_secs:.1f}s de audio al servidor...", flush=True)
    try:
        resp = requests.post(
            API_URL,
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
            timeout=90,
        )
        if resp.ok:
            data = resp.json()
            if data.get("transcripcion"):
                print(f"\n📝 Transcripción:\n{data['transcripcion']}")
            if data.get("decisiones"):
                print(f"\n✅ Decisiones:\n{data['decisiones']}")
        else:
            print(f"⚠️  Servidor respondió {resp.status_code}: {resp.text[:300]}")
    except requests.exceptions.Timeout:
        print("⚠️  Timeout al conectar con el servidor.")
    except Exception as exc:
        print(f"⚠️  Error enviando: {exc}")
    finally:
        print("\n🎙️  Escuchando...\n", flush=True)


# ── Loop principal ────────────────────────────────────────────────────────────

def run() -> None:
    audio_q: queue.Queue[bytes] = queue.Queue()

    voiced:  list[bytes] = []   # frames de voz activa
    silence: list[bytes] = []   # frames de silencio tras voz (ventana de espera)
    recording = False

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"⚠️  sounddevice: {status}", file=sys.stderr)
        audio_q.put(bytes(indata))

    print(f"🎙️  Escuchando... (umbral RMS={RMS_THRESHOLD}, Ctrl+C para detener)\n", flush=True)

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            frame = audio_q.get()
            is_speech = frame_rms(frame) >= RMS_THRESHOLD

            if is_speech:
                if not recording:
                    recording = True
                    print("🔴 Voz detectada — grabando...", flush=True)
                # Incluir silencios internos (pausas naturales del hablante)
                voiced.extend(silence)
                silence.clear()
                voiced.append(frame)

            else:
                if recording:
                    silence.append(frame)

                    if len(silence) >= SILENCE_FRAMES:
                        # 2 segundos de silencio → cerrar bloque
                        recording = False
                        voz_secs  = len(voiced) * FRAME_MS / 1000

                        if len(voiced) >= MIN_VOICE_FRAMES:
                            wav = frames_to_wav(voiced)
                            threading.Thread(
                                target=send_audio,
                                args=(wav, voz_secs),
                                daemon=True,
                            ).start()
                        else:
                            print(f"⏭️  Descartado ({voz_secs:.1f}s — muy corto)\n", flush=True)
                            print("🎙️  Escuchando...\n", flush=True)

                        voiced.clear()
                        silence.clear()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n\nDetenido.")
