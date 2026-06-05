import sys
import json
import wave
import os
import urllib.request
from datetime import date
import openai
import anthropic
from dotenv import load_dotenv

load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

AUDIO_FILE = "prueba simulando kiosco.m4a"
SAMPLE_RATE = 16000

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def saludo_voz(temp_max, es_dia_cobro):
    SALUDO_WAV = "saludo.wav"

    try:
        if es_dia_cobro:
            texto_saludo = (
                f"¡Buenos días, Martina! "
                f"Hoy la temperatura máxima va a ser de {temp_max} grados. "
                f"Es día de cobro, así que se espera buen movimiento en el kiosco. "
                f"¡Que tengas un excelente día!"
            )
        else:
            texto_saludo = (
                f"¡Buenos días, Martina! "
                f"Hoy la temperatura máxima va a ser de {temp_max} grados. "
                f"No es día de cobro, pero igual a darle con todo. "
                f"¡Que tengas un excelente día!"
            )

        print(f"Saludo: {texto_saludo}")
        print("Generando saludo TTS...")

        respuesta_tts = openai_client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=texto_saludo,
            response_format="wav"
        )

        with open(SALUDO_WAV, "wb") as f:
            f.write(respuesta_tts.content)

        print("Reproduciendo saludo...")
        os.startfile(SALUDO_WAV)

    except Exception as e:
        print(f"Error en saludo_voz(): {e}")


def cargar_stock():
    import numpy as np
    import sounddevice as sd

    DURACION = 60
    TEMP_WAV = "temp_stock.wav"

    print(f"\nGrabando {DURACION} segundos... hablá el stock del kiosco.")
    audio = sd.rec(int(DURACION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("Grabación terminada.")

    with wave.open(TEMP_WAV, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    print("Transcribiendo stock...")
    with open(TEMP_WAV, "rb") as f:
        transcripcion = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="es"
        )
    os.remove(TEMP_WAV)

    texto_stock = transcripcion.text
    print(f"Transcripción: {texto_stock}\n")

    respuesta = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "Sos un asistente de kiosco argentino. Extraés productos y cantidades de texto hablado. "
            "Devolvés ÚNICAMENTE un JSON válido con el formato {\"producto\": cantidad} donde cantidad es un número entero. "
            "Sin explicaciones, sin markdown, solo el JSON."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Extraé todos los productos con sus cantidades de este texto y devolvé solo el JSON:\n\n{texto_stock}"
            }
        ]
    )

    json_texto = respuesta.content[0].text.strip()
    if "```" in json_texto:
        json_texto = json_texto.split("```")[1]
        if json_texto.startswith("json"):
            json_texto = json_texto[4:]
        json_texto = json_texto.strip()

    stock = json.loads(json_texto)

    with open("stock.json", "w", encoding="utf-8") as f:
        json.dump(stock, f, ensure_ascii=False, indent=2)

    print("--- STOCK CARGADO ---")
    for producto, cantidad in stock.items():
        print(f"  {producto}: {cantidad}")
    print("Stock guardado en stock.json\n")


# ── Inicio ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # 1. Clima de Lomas de Zamora
    print("Consultando clima...")
    weather_url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=-34.76&longitude=-58.40"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        "&timezone=America%2FArgentina%2FBuenos_Aires"
        "&forecast_days=2"
    )
    with urllib.request.urlopen(weather_url) as resp:
        weather = json.loads(resp.read())

    d = weather["daily"]
    temp_max_hoy = d["temperature_2m_max"][0]
    clima_hoy = (
        f"Hoy: máx {temp_max_hoy}°C, "
        f"mín {d['temperature_2m_min'][0]}°C, "
        f"lluvia {d['precipitation_sum'][0]} mm"
    )
    clima_manana = (
        f"Mañana: máx {d['temperature_2m_max'][1]}°C, "
        f"mín {d['temperature_2m_min'][1]}°C, "
        f"lluvia {d['precipitation_sum'][1]} mm"
    )
    print(clima_hoy)
    print(clima_manana)

    # 2. Detección de día de cobro
    hoy = date.today()
    dia = hoy.day
    es_dia_cobro = (1 <= dia <= 5) or (15 <= dia <= 20)
    contexto_cobro = (
        f"Hoy es {hoy.strftime('%d/%m/%Y')} — DÍA DE COBRO (quincena o fin de mes)."
        if es_dia_cobro else
        f"Hoy es {hoy.strftime('%d/%m/%Y')} — no es día de cobro."
    )
    print(contexto_cobro)

    # 3. Saludo por voz
    saludo_voz(temp_max_hoy, es_dia_cobro)

    # 4. Stock
    respuesta_stock = input("\n¿Querés actualizar el stock? (s/n): ").strip().lower()
    if respuesta_stock == "s":
        cargar_stock()

    # 5. Transcripción con Whisper
    print("\nTranscribiendo audio...")
    with open(AUDIO_FILE, "rb") as audio_file:
        transcripcion = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="es"
        )

    texto = transcripcion.text
    print("\n--- TRANSCRIPCIÓN ---")
    print(texto)

    # 6. Decisiones con Claude
    print("\nGenerando decisiones...")
    contexto_extra = (
        f"CONTEXTO DEL DÍA:\n"
        f"- {clima_hoy}\n"
        f"- {clima_manana}\n"
        f"- {contexto_cobro}\n"
    )

    respuesta = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "Sos el asistente inteligente de un kiosco argentino. "
            "Tu rol es analizar conversaciones con clientes y generar decisiones concretas y accionables "
            "para mejorar la atención, las ventas o la operación del kiosco. "
            "Usá el contexto climático y de día de cobro para hacer recomendaciones más precisas. "
            "Siempre respondé en español rioplatense y sé directo y práctico."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"{contexto_extra}\n"
                    f"Analizá esta conversación de un kiosco y generá exactamente 5 decisiones concretas "
                    f"basadas en lo que escuchaste y en el contexto del día:\n\n{texto}"
                )
            }
        ]
    )

    decisiones = respuesta.content[0].text
    print("\n--- 5 DECISIONES ---")
    print(decisiones)

    with open("decisiones.txt", "w", encoding="utf-8") as f:
        f.write(decisiones)
    print("\nDecisiones guardadas en decisiones.txt")
