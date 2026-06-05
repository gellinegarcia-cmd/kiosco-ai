import os
import tempfile
from flask import Flask, request, jsonify
from pipeline import openai_client, anthropic_client

app = Flask(__name__)

DECISIONES_FILE = "decisiones.txt"

SYSTEM_PROMPT = (
    "Sos el asistente inteligente de un kiosco argentino. "
    "Tu rol es analizar conversaciones con clientes y generar decisiones concretas y accionables "
    "para mejorar la atención, las ventas o la operación del kiosco. "
    "Siempre respondé en español rioplatense y sé directo y práctico."
)


@app.route("/audio", methods=["POST"])
def procesar_audio():
    if "audio" not in request.files:
        return jsonify({"error": "No se recibió archivo de audio. Enviá el archivo con el campo 'audio'."}), 400

    archivo = request.files["audio"]
    sufijo = os.path.splitext(archivo.filename)[1] or ".m4a"

    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        archivo.save(tmp.name)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            transcripcion = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="es"
            )
        texto = transcripcion.text

        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Analizá esta conversación de un kiosco y generá exactamente 5 decisiones concretas "
                        f"basadas en lo que escuchaste:\n\n{texto}"
                    )
                }
            ]
        )
        decisiones = respuesta.content[0].text

        with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
            f.write(decisiones)

        return jsonify({
            "transcripcion": texto,
            "decisiones": decisiones
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        os.remove(tmp_path)


@app.route("/decisiones", methods=["GET"])
def get_decisiones():
    if not os.path.exists(DECISIONES_FILE):
        return jsonify({"decisiones": None, "mensaje": "No hay decisiones generadas aún."}), 404

    with open(DECISIONES_FILE, "r", encoding="utf-8") as f:
        contenido = f.read()

    return jsonify({"decisiones": contenido})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", debug=True, port=port)
