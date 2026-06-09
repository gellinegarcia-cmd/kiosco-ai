import os
import json
import tempfile
import openai
import anthropic
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

DECISIONES_FILE = "decisiones.txt"

BASE_SYSTEM_PROMPT = (
    "Sos el asistente inteligente de un negocio argentino. "
    "Tu rol es analizar conversaciones con clientes y generar decisiones concretas y accionables "
    "para mejorar la atención, las ventas o la operación del negocio. "
    "Siempre respondé en español rioplatense y sé directo y práctico."
)


def construir_system_prompt(perfil=None):
    if not perfil:
        return BASE_SYSTEM_PROMPT

    partes = []
    if perfil.get("nombre"):
        partes.append(f"Nombre del negocio: {perfil['nombre']}")
    if perfil.get("tipo"):
        partes.append(f"Tipo de negocio: {perfil['tipo']}")
    if perfil.get("barrio"):
        partes.append(f"Ubicación: {perfil['barrio']}")
    if perfil.get("clientes"):
        c = perfil["clientes"]
        clientes_str = ", ".join(c) if isinstance(c, list) else str(c)
        partes.append(f"Clientes principales: {clientes_str}")
    if perfil.get("productos"):
        p = perfil["productos"]
        productos_str = ", ".join(p) if isinstance(p, list) else str(p)
        partes.append(f"Productos más vendidos: {productos_str}")

    if not partes:
        return BASE_SYSTEM_PROMPT

    perfil_str = "\n".join(f"  - {p}" for p in partes)
    return BASE_SYSTEM_PROMPT + f"\n\nPERFIL DEL NEGOCIO:\n{perfil_str}"


def parse_perfil(perfil_raw):
    if not perfil_raw:
        return None
    if isinstance(perfil_raw, dict):
        return perfil_raw
    try:
        return json.loads(perfil_raw)
    except Exception:
        return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/audio", methods=["POST"])
def procesar_audio():
    print("[/audio] Request recibido", flush=True)

    if "audio" not in request.files:
        print("[/audio] ERROR: campo 'audio' ausente en el request", flush=True)
        return jsonify({"error": "No se recibió archivo de audio. Enviá el archivo con el campo 'audio'."}), 400

    archivo = request.files["audio"]
    sufijo = os.path.splitext(archivo.filename)[1] or ".m4a"
    perfil = parse_perfil(request.form.get("perfil"))

    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        archivo.save(tmp.name)
        tmp_path = tmp.name

    tamanio_kb = os.path.getsize(tmp_path) / 1024
    print(f"[/audio] Archivo recibido: {archivo.filename!r} | {tamanio_kb:.1f} KB | perfil: {bool(perfil)}", flush=True)

    if tamanio_kb < 1:
        os.remove(tmp_path)
        print("[/audio] Archivo demasiado pequeño, descartado", flush=True)
        return jsonify({"error": "Archivo de audio demasiado pequeño."}), 400

    try:
        print("[/audio] Transcribiendo con Whisper...", flush=True)
        openai_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        with open(tmp_path, "rb") as f:
            transcripcion = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="es"
            )
        texto = transcripcion.text
        print(f"[/audio] Transcripción ({len(texto)} chars): {texto[:120]!r}", flush=True)

        system_prompt = construir_system_prompt(perfil)

        print("[/audio] Generando decisiones con Claude...", flush=True)
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Analizá esta conversación del negocio y generá exactamente 5 decisiones concretas "
                        f"basadas en lo que escuchaste:\n\n{texto}"
                    )
                }
            ]
        )
        decisiones = respuesta.content[0].text

        with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
            f.write(decisiones)

        print("[/audio] OK — respuesta enviada", flush=True)
        return jsonify({"transcripcion": texto, "decisiones": decisiones})

    except Exception as e:
        print(f"[/audio] ERROR: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/texto", methods=["POST"])
def procesar_texto():
    data = request.get_json(silent=True)
    texto = (data or {}).get("mensaje") or (data or {}).get("texto", "")
    if not texto.strip():
        return jsonify({"error": "Enviá JSON con campo 'mensaje' no vacío."}), 400

    texto = texto.strip()
    perfil = parse_perfil((data or {}).get("perfil"))

    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

        system_prompt = construir_system_prompt(perfil)

        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Analizá esta situación del negocio y generá exactamente 5 decisiones concretas "
                        f"basadas en lo que te escriben:\n\n{texto}"
                    )
                }
            ]
        )
        decisiones = respuesta.content[0].text

        with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
            f.write(decisiones)

        return jsonify({"texto": texto, "decisiones": decisiones})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/decisiones", methods=["GET"])
def get_decisiones():
    if not os.path.exists(DECISIONES_FILE):
        return jsonify({"decisiones": None, "mensaje": "No hay decisiones generadas aún."}), 404

    with open(DECISIONES_FILE, "r", encoding="utf-8") as f:
        contenido = f.read()

    return jsonify({"decisiones": contenido})
