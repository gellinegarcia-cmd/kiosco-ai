import os
import json
import tempfile
import threading
import openai
import anthropic
import gspread
from datetime import date, datetime, timezone, timedelta
from google.oauth2.service_account import Credentials
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

DECISIONES_FILE = "decisiones.txt"
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GSHEETS_SCOPES  = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BASE_SYSTEM_PROMPT = (
    "Sos el asistente inteligente de un negocio argentino. "
    "Tu rol es analizar conversaciones con clientes y generar decisiones concretas y accionables "
    "para mejorar la atención, las ventas o la operación del negocio. "
    "Siempre respondé en español rioplatense y sé directo y práctico."
)


# ── Helpers generales ─────────────────────────────────────────────────────────

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
        partes.append(f"Clientes principales: {', '.join(c) if isinstance(c, list) else c}")
    if perfil.get("productos"):
        p = perfil["productos"]
        partes.append(f"Productos más vendidos: {', '.join(p) if isinstance(p, list) else p}")
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


# ── Google Sheets ─────────────────────────────────────────────────────────────

CONFIG_HEADERS  = ["activo", "hora_apertura", "hora_cierre", "descanso",
                   "hora_siesta_inicio", "hora_siesta_fin", "saludo_automatico"]
CONFIG_DEFAULTS = {
    "activo":             "false",
    "hora_apertura":      "09:00",
    "hora_cierre":        "20:00",
    "descanso":           "false",
    "hora_siesta_inicio": "13:00",
    "hora_siesta_fin":    "14:00",
    "saludo_automatico":  "false",
}


def _gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        creds = Credentials.from_service_account_info(
            json.loads(creds_json), scopes=GSHEETS_SCOPES
        )
    else:
        creds = Credentials.from_service_account_file(
            "credentials.json", scopes=GSHEETS_SCOPES
        )
    return gspread.authorize(creds)


def get_sheet():
    """Devuelve la hoja 'Sheet1' (contexto/transcripciones)."""
    client = _gspread_client()
    ws = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    if not ws.get_all_values():
        ws.append_row(["timestamp", "transcripcion"])
    return ws


def get_config_sheet():
    """Devuelve la hoja 'config', creándola con defaults si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("config")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="config", rows=2,
                                       cols=len(CONFIG_HEADERS))
        ws.append_row(CONFIG_HEADERS)
        ws.append_row([CONFIG_DEFAULTS[h] for h in CONFIG_HEADERS])
    return ws


def leer_config(ws):
    """Lee la fila de valores de la hoja config y devuelve un dict."""
    todas = ws.get_all_values()
    if len(todas) < 2:
        return dict(CONFIG_DEFAULTS)
    headers = todas[0]
    valores = todas[1]
    return {h: (valores[i] if i < len(valores) else CONFIG_DEFAULTS.get(h, ""))
            for i, h in enumerate(headers)}


def calcular_debe_grabar(cfg):
    """Devuelve True si el asistente debe estar grabando ahora (hora Argentina)."""
    if cfg.get("activo", "false").lower() != "true":
        return False

    tz_ar = timezone(timedelta(hours=-3))
    ahora = datetime.now(tz_ar)
    cur   = ahora.hour * 60 + ahora.minute

    def hm(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    if not (hm(cfg.get("hora_apertura", "09:00")) <= cur
            < hm(cfg.get("hora_cierre", "20:00"))):
        return False

    if cfg.get("descanso", "false").lower() == "true":
        if (hm(cfg.get("hora_siesta_inicio", "13:00")) <= cur
                < hm(cfg.get("hora_siesta_fin", "14:00"))):
            return False

    return True


def get_filas_hoy(ws):
    """Devuelve las filas de hoy como lista de [timestamp, transcripcion]."""
    hoy = date.today().isoformat()          # "2026-06-09"
    todas = ws.get_all_values()
    # Saltar fila de encabezado
    datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
    return [f for f in datos if len(f) >= 2 and f[0].startswith(hoy)]


# ── Background para /analizar ─────────────────────────────────────────────────

def _analizar_en_background(contexto_texto, system_prompt, n_fragmentos):
    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        print("[/analizar] Generando decisiones con Claude...", flush=True)
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    "A continuación están todas las conversaciones registradas en el negocio "
                    "durante el día de hoy, con su hora. Analizalas en conjunto y generá entre "
                    "8 y 10 decisiones detalladas y accionables para mejorar la operación del "
                    "negocio. Considerá patrones, horarios pico, productos mencionados y "
                    "necesidades recurrentes de los clientes.\n\n"
                    f"CONVERSACIONES DEL DÍA:\n{contexto_texto}"
                )
            }]
        )
        decisiones = respuesta.content[0].text
        with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
            f.write(decisiones)
        print(f"[/analizar] OK — {n_fragmentos} fragmentos analizados, decisiones guardadas", flush=True)
    except Exception as e:
        print(f"[/analizar] ERROR en background: {type(e).__name__}: {e}", flush=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/config", methods=["GET"])
def get_config():
    try:
        ws  = get_config_sheet()
        cfg = leer_config(ws)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ua = request.headers.get("User-Agent", "")

    def b(k): return cfg.get(k, "false").lower() == "true"
    return jsonify({
        "activo":            b("activo"),
        "apertura":          cfg.get("hora_apertura",      "09:00"),
        "cierre":            cfg.get("hora_cierre",        "20:00"),
        "descanso":          b("descanso"),
        "descanso_inicio":   cfg.get("hora_siesta_inicio", "13:00"),
        "descanso_fin":      cfg.get("hora_siesta_fin",    "14:00"),
        "saludo_automatico": b("saludo_automatico"),
        "debe_grabar":       calcular_debe_grabar(cfg),
        "es_dispositivo":    ua.startswith("okhttp"),
    })


@app.route("/config", methods=["POST"])
def set_config():
    data = request.get_json(silent=True) or {}

    def bool_str(v):
        return "true" if (v is True or str(v).lower() == "true") else "false"

    nuevos = {
        "activo":             bool_str(data.get("activo",            False)),
        "hora_apertura":      data.get("apertura",          "09:00"),
        "hora_cierre":        data.get("cierre",            "20:00"),
        "descanso":           bool_str(data.get("descanso",          False)),
        "hora_siesta_inicio": data.get("descanso_inicio",   "13:00"),
        "hora_siesta_fin":    data.get("descanso_fin",      "14:00"),
        "saludo_automatico":  bool_str(data.get("saludo_automatico", False)),
    }

    try:
        ws    = get_config_sheet()
        todas = ws.get_all_values()
        if todas:
            headers = todas[0]
            fila    = [nuevos.get(h, "") for h in headers]
            col_fin = chr(ord("A") + len(headers) - 1)
            if len(todas) >= 2:
                ws.update(f"A2:{col_fin}2", [fila])
            else:
                ws.append_row(fila)
        else:
            ws.append_row(CONFIG_HEADERS)
            ws.append_row([nuevos[h] for h in CONFIG_HEADERS])
        print(f"[/config POST] Guardado: {nuevos}", flush=True)
    except Exception as e:
        print(f"[/config POST] ERROR: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

    def b(k): return nuevos[k] == "true"
    return jsonify({
        "activo":            b("activo"),
        "apertura":          nuevos["hora_apertura"],
        "cierre":            nuevos["hora_cierre"],
        "descanso":          b("descanso"),
        "descanso_inicio":   nuevos["hora_siesta_inicio"],
        "descanso_fin":      nuevos["hora_siesta_fin"],
        "saludo_automatico": b("saludo_automatico"),
        "debe_grabar":       calcular_debe_grabar(nuevos),
    })


@app.route("/audio", methods=["POST"])
def procesar_audio():
    print("[/audio] Request recibido", flush=True)

    if "audio" not in request.files:
        print("[/audio] ERROR: campo 'audio' ausente", flush=True)
        return jsonify({"error": "No se recibió archivo de audio. Enviá el archivo con el campo 'audio'."}), 400

    archivo = request.files["audio"]
    sufijo  = os.path.splitext(archivo.filename)[1] or ".m4a"

    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        archivo.save(tmp.name)
        tmp_path = tmp.name

    tamanio_kb = os.path.getsize(tmp_path) / 1024
    print(f"[/audio] Archivo: {archivo.filename!r} | {tamanio_kb:.1f} KB", flush=True)

    if tamanio_kb < 1:
        os.remove(tmp_path)
        print("[/audio] Archivo demasiado pequeño, descartado", flush=True)
        return jsonify({"error": "Archivo de audio demasiado pequeño."}), 400

    try:
        print("[/audio] Transcribiendo con Whisper...", flush=True)
        openai_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        with open(tmp_path, "rb") as f:
            transcripcion = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="es"
            )
        texto = transcripcion.text.strip()
        print(f"[/audio] Transcripción ({len(texto)} chars): {texto[:120]!r}", flush=True)

        if texto:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws = get_sheet()
            ws.append_row([timestamp, texto])
            print(f"[/audio] Fila agregada a Google Sheets: [{timestamp}]", flush=True)
        else:
            print("[/audio] Transcripción vacía, no se guarda", flush=True)

        print("[/audio] OK", flush=True)
        return jsonify({"transcripcion": texto, "acumulado": bool(texto)})

    except Exception as e:
        print(f"[/audio] ERROR: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/analizar", methods=["GET"])
def analizar():
    print("[/analizar] Request recibido", flush=True)
    try:
        ws = get_sheet()
        filas_hoy = get_filas_hoy(ws)
    except Exception as e:
        print(f"[/analizar] ERROR leyendo Sheets: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"Error accediendo a Google Sheets: {e}"}), 500

    if not filas_hoy:
        return jsonify({"error": "No hay contexto acumulado hoy."}), 404

    if len(filas_hoy) > 50:
        print(f"[/analizar] {len(filas_hoy)} fragmentos — limitando a los últimos 50", flush=True)
        filas_hoy = filas_hoy[-50:]

    n = len(filas_hoy)
    contexto_texto = "\n".join(f"[{f[0]}] {f[1]}" for f in filas_hoy)
    print(f"[/analizar] Analizando {n} fragmentos en background", flush=True)

    perfil       = parse_perfil(request.args.get("perfil"))
    system_prompt = construir_system_prompt(perfil)

    threading.Thread(
        target=_analizar_en_background,
        args=(contexto_texto, system_prompt, n),
        daemon=True
    ).start()

    return jsonify({"status": "procesando", "fragmentos": n})


@app.route("/contexto", methods=["GET"])
def ver_contexto():
    try:
        ws = get_sheet()
        filas_hoy = get_filas_hoy(ws)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    contexto_texto = "\n".join(f"[{f[0]}] {f[1]}" for f in filas_hoy)
    return jsonify({"contexto": contexto_texto, "fragmentos": len(filas_hoy)})


@app.route("/contexto", methods=["DELETE"])
def limpiar_contexto():
    print("[/contexto DELETE] Eliminando filas de hoy de Google Sheets", flush=True)
    try:
        ws = get_sheet()
        hoy  = date.today().isoformat()
        todas = ws.get_all_values()
        # Índices 1-based de filas de hoy (saltando encabezado en fila 1)
        indices = [
            i + 1
            for i, fila in enumerate(todas)
            if i > 0 and fila and fila[0].startswith(hoy)
        ]
        if not indices:
            return jsonify({"mensaje": "No hay filas de hoy para eliminar.", "eliminadas": 0})
        # Borrar en bloque (las filas de hoy son siempre las últimas)
        ws.delete_rows(min(indices), max(indices))
        print(f"[/contexto DELETE] {len(indices)} filas eliminadas", flush=True)
        return jsonify({"mensaje": f"Eliminadas {len(indices)} filas de hoy.", "eliminadas": len(indices)})
    except Exception as e:
        print(f"[/contexto DELETE] ERROR: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/texto", methods=["POST"])
def procesar_texto():
    data   = request.get_json(silent=True)
    texto  = (data or {}).get("mensaje") or (data or {}).get("texto", "")
    if not texto.strip():
        return jsonify({"error": "Enviá JSON con campo 'mensaje' no vacío."}), 400

    texto  = texto.strip()
    perfil = parse_perfil((data or {}).get("perfil"))

    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        system_prompt    = construir_system_prompt(perfil)
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    f"Analizá esta situación del negocio y generá exactamente 5 decisiones concretas "
                    f"basadas en lo que te escriben:\n\n{texto}"
                )
            }]
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
