import sys
import os
import json
import wave
import urllib.request
from datetime import datetime, date
import numpy as np
import sounddevice as sd
from pipeline import saludo_voz, openai_client, anthropic_client

sys.stdout.reconfigure(encoding="utf-8")

SAMPLE_RATE = 16000
DURACION_BLOQUE = 1 * 60  # 1 minuto en segundos
BLOQUES_POR_DECISION = 2
HORA_RESET = 20
CONTEXTO_FILE = "contexto_dia.txt"
DECISIONES_FILE = "decisiones.txt"
TEMP_WAV = "temp_bloque.wav"


def obtener_clima():
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=-34.76&longitude=-58.40"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        "&timezone=America%2FArgentina%2FBuenos_Aires"
        "&forecast_days=2"
    )
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read())
    return data["daily"]["temperature_2m_max"][0]


def grabar_bloque():
    print(f"Grabando {DURACION_BLOQUE // 60} minutos...")
    audio = sd.rec(int(DURACION_BLOQUE * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("Grabación terminada.")

    with wave.open(TEMP_WAV, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    with open(TEMP_WAV, "rb") as f:
        transcripcion = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="es"
        )
    os.remove(TEMP_WAV)
    return transcripcion.text


def generar_decisiones():
    if not os.path.exists(CONTEXTO_FILE):
        return

    with open(CONTEXTO_FILE, "r", encoding="utf-8") as f:
        contexto = f.read().strip()

    if not contexto:
        return

    hoy = date.today()
    dia = hoy.day
    es_dia_cobro = (1 <= dia <= 5) or (15 <= dia <= 20)
    contexto_cobro = "DÍA DE COBRO" if es_dia_cobro else "no es día de cobro"

    print("\nGenerando decisiones con Claude...")
    respuesta = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "Sos el asistente inteligente de un kiosco argentino. "
            "Analizás conversaciones acumuladas del día y generás decisiones concretas y accionables. "
            "Siempre respondé en español rioplatense y sé directo y práctico."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Contexto acumulado del día ({hoy.strftime('%d/%m/%Y')}, {contexto_cobro}):\n\n"
                    f"{contexto}\n\n"
                    f"Analizá todo lo que pasó en el kiosco hoy y generá exactamente 5 decisiones concretas y actualizadas."
                )
            }
        ]
    )

    decisiones = respuesta.content[0].text
    timestamp = datetime.now().strftime("%H:%M:%S")

    with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
        f.write(f"Decisiones actualizadas al {timestamp}\n\n{decisiones}")

    print(f"\n--- DECISIONES ACTUALIZADAS ({timestamp}) ---")
    print(decisiones)
    print(f"\nGuardadas en {DECISIONES_FILE}")


# ── Inicio ──────────────────────────────────────────────────────────────────

print("Iniciando kiosco-ai escucha continua...")

try:
    temp_max = obtener_clima()
    hoy = date.today()
    dia = hoy.day
    es_dia_cobro = (1 <= dia <= 5) or (15 <= dia <= 20)
    saludo_voz(temp_max, es_dia_cobro)
except Exception as e:
    print(f"Error en saludo inicial: {e}")

bloque_num = 0
ultimo_reset = None

while True:
    now = datetime.now()

    # Reset a las 20hs, una sola vez por día
    if now.hour == HORA_RESET and ultimo_reset != now.date():
        print(f"\n[{now.strftime('%H:%M')}] Limpiando contexto del día para mañana...")
        open(CONTEXTO_FILE, "w", encoding="utf-8").close()
        ultimo_reset = now.date()
        bloque_num = 0
        print("Contexto limpiado.")

    timestamp = now.strftime("%H:%M:%S")
    print(f"\n[{timestamp}] Bloque {bloque_num + 1}")

    try:
        texto = grabar_bloque()

        if texto.strip():
            with open(CONTEXTO_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n[{timestamp}]\n{texto}\n")
            print(f"Transcripción agregada a {CONTEXTO_FILE}")
        else:
            print("Bloque sin audio detectable.")

        bloque_num += 1

        if bloque_num % BLOQUES_POR_DECISION == 0:
            generar_decisiones()

    except Exception as e:
        print(f"Error en bloque {bloque_num + 1}: {e}")
