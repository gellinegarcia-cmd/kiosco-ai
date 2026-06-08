#!/usr/bin/env python3
"""
vad_recorder.py — Grabación con VAD usando webrtcvad + sounddevice

Escucha el micrófono en tiempo real, detecta voz humana y envía el audio
al servidor cuando hay 2 segundos de silencio consecutivo.

Instalación:
    pip install sounddevice requests python-dotenv
    pip install webrtcvad-wheels   # Windows (incluye binarios precompilados)
    pip install webrtcvad          # Linux / Mac

Nota: OPENAI_API_KEY y ANTHROPIC_API_KEY son usadas por el servidor,
      no por este script. Se cargan desde .env por coherencia con el proyecto.
"""

import io
import os
import queue
import sys
import threading
import wave

import requests
import sounddevice as sd
import webrtcvad
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────

API_URL            = "https://kiosco-ai.onrender.com/audio"
SAMPLE_RATE        = 16_000   # Hz — obligatorio para webrtcvad
FRAME_MS           = 30       # duración de frame: 10, 20 o 30 ms
FRAME_SAMPLES      = int(SAMPLE_RATE * FRAME_MS / 1000)   # 480 muestras
VAD_AGGRESSIVENESS = 2        # 0 = permisivo … 3 = muy estricto
SILENCE_SECS       = 2.0      # segundos de silencio para cerrar bloque
MIN_VOICE_SECS     = 0.5      # mínimo de voz para no descartar

SILENCE_FRAMES  = int(SILENCE_SECS * 1000 / FRAME_MS)
MIN_VOICE_FRAMES = int(MIN_VOICE_SECS * 1000 / FRAME_MS)

# ── Helpers ───────────────────────────────────────────────────────────────────

def frames_to_wav(frames: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)       # 16-bit PCM
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def send_audio(wav_bytes: bytes, voz_secs: float) -> None:
    """Envía el WAV al servidor en un hilo separado."""
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
    vad         = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    audio_q     = queue.Queue()

    voiced: list[bytes]  = []   # frames de voz confirmada
    silence: list[bytes] = []   # frames de silencio tras voz (ventana de espera)
    recording            = False

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(f"⚠️  sounddevice: {status}", file=sys.stderr)
        audio_q.put(bytes(indata))

    print("🎙️  Escuchando... (Ctrl+C para detener)\n", flush=True)

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=FRAME_SAMPLES,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        while True:
            frame = audio_q.get()

            try:
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
            except Exception:
                continue    # frame corrupto (longitud incorrecta), ignorar

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
                            print(
                                f"⏭️  Descartado ({voz_secs:.1f}s — muy corto)\n",
                                flush=True,
                            )
                            print("🎙️  Escuchando...\n", flush=True)

                        voiced.clear()
                        silence.clear()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n\nDetenido.")
