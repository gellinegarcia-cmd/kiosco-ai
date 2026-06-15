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

BASE_SYSTEM_PROMPT = """\
Sos Gelline, el socio silencioso de este negocio argentino. Cada día escuchás lo que pasa en el local y le contás al dueño lo más importante. Hablás en español rioplatense, directo y cálido, sin jerga técnica. Sos el mismo Gelline que hace el resumen semanal — mantenés la misma voz y la misma relación de confianza día a día.

REGLAS:
- Ignorá conversaciones que no son del negocio (charlas personales, TV, ruido)
- Priorizá por impacto económico
- Generá EXACTAMENTE 3 decisiones: una urgente, una importante, una de reflexión — nunca más de una por categoría"""

DECISIONES_USER_PROMPT = """\
Analizá las conversaciones del negocio y generá decisiones accionables organizadas en estas tres categorías, usando exactamente este formato para cada una:

### urgente
💔 **Lo que pasó:** [situación concreta detectada]
💡 **Lo que podés hacer:** [acción específica realizable hoy o mañana]
✅ **La decisión:** [resumen en una oración]

### importante
💔 **Lo que pasó:** [situación concreta detectada]
💡 **Lo que podés hacer:** [acción para esta semana]
✅ **La decisión:** [resumen en una oración]

### reflexión
💔 **Lo que pasó:** [patrón o situación estructural]
💡 **Lo que podés hacer:** [cambio a mediano plazo]
✅ **La decisión:** [resumen en una oración]

Generá exactamente una decisión por categoría — nunca más de 3 en total. Si hay varias situaciones urgentes, elegí la de mayor impacto económico y dejá el resto para el informe semanal. No agregues título general, texto introductorio ni conclusión al final.

CONVERSACIONES:
{conversaciones}"""

WEEKLY_SYSTEM_PROMPT = """\
Sos Gelline, el socio silencioso de este negocio argentino. Hablás en español rioplatense, directo y cálido, sin jerga técnica. Sos el mismo Gelline del resumen diario.

Tu trabajo es detectar CAMBIOS respecto a patrones anteriores — no repetir información ya conocida. Si algo siempre fue igual, no lo menciones. Solo importa lo que es diferente.

Ignorá conversaciones que no son del negocio (charlas personales, TV, ruido).

FORMATO OBLIGATORIO — respondé ÚNICAMENTE con este formato, sin texto antes ni después:

## LO QUE CAMBIÓ
3 a 5 cosas que cambiaron esta semana respecto a lo normal: productos que dejaron de pedirse, quejas nuevas, caídas o subas de actividad en algún horario, algo que antes funcionaba y ahora no. Si tenés el informe de la semana anterior, compará explícitamente.

## LO QUE SE MANTIENE FUERTE
3 a 5 cosas que siguen funcionando bien, igual o mejor que antes. Lo que no hay que tocar.

## 3 ACCIONES PARA ESTA SEMANA
### 1. Acción concreta con verbo y fecha
Una oración explicando por qué, conectada a lo que cambió.
### 2. Acción concreta con verbo y fecha
Una oración explicando por qué.
### 3. Acción concreta con verbo y fecha
Una oración explicando por qué."""

WEEKLY_SYSTEM_PROMPT_PRIMERA_SEMANA = WEEKLY_SYSTEM_PROMPT + (
    "\n\nEsta es la primera semana, no hay informe anterior para comparar — "
    "usá el formato pero la sección LO QUE CAMBIÓ puede estar vacía o decir que es la línea de base."
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
    hoy = date.today().isoformat()
    todas = ws.get_all_values()
    datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
    return [f for f in datos if len(f) >= 2 and f[0].startswith(hoy)]


def get_filas_semana(ws):
    """Devuelve las filas de los últimos 7 días como lista de [timestamp, transcripcion]."""
    hoy = date.today()
    fechas = {(hoy - timedelta(days=i)).isoformat() for i in range(7)}
    todas = ws.get_all_values()
    datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
    return [f for f in datos if len(f) >= 2 and any(f[0].startswith(d) for d in fechas)]


def get_decisiones_semana_sheet():
    """Devuelve la hoja 'decisiones_semana', creándola con encabezado si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        return spreadsheet.worksheet("decisiones_semana")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="decisiones_semana", rows=100, cols=2)
        ws.append_row(["timestamp", "informe"])
        return ws


def get_ultimo_informe_semanal():
    """Devuelve el texto del último informe semanal guardado, o None si no hay ninguno."""
    try:
        ws = get_decisiones_semana_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        if not datos:
            return None
        return datos[-1][1] if len(datos[-1]) >= 2 else None
    except Exception as e:
        print(f"[get_ultimo_informe_semanal] ERROR: {e}", flush=True)
        return None


# ── Background para /analizar ─────────────────────────────────────────────────

def _analizar_en_background(contexto_texto, system_prompt, n_fragmentos, periodo="hoy", informe_anterior=None):
    tag = f"[/analizar {periodo}]"
    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        print(f"{tag} Generando decisiones con Claude...", flush=True)

        if periodo == "semana":
            if informe_anterior:
                user_content = (
                    f"INFORME DE LA SEMANA ANTERIOR:\n{informe_anterior}\n\n"
                    f"CONVERSACIONES DE ESTA SEMANA:\n{contexto_texto}"
                )
            else:
                user_content = f"CONVERSACIONES DE ESTA SEMANA:\n{contexto_texto}"
        else:
            user_content = DECISIONES_USER_PROMPT.format(
                conversaciones=f"CONVERSACIONES DEL DÍA:\n{contexto_texto}"
            )

        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )
        decisiones = respuesta.content[0].text

        if periodo == "semana":
            ws = get_decisiones_semana_sheet()
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws.append_row([ts, decisiones])
            print(f"{tag} OK — {n_fragmentos} fragmentos, informe guardado en Sheets", flush=True)
        else:
            with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
                f.write(decisiones)
            print(f"{tag} OK — {n_fragmentos} fragmentos, decisiones guardadas", flush=True)
    except Exception as e:
        print(f"{tag} ERROR en background: {type(e).__name__}: {e}", flush=True)


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
    periodo = request.args.get("periodo", "hoy")
    print(f"[/analizar] Request recibido — periodo={periodo}", flush=True)
    try:
        ws    = get_sheet()
        filas = get_filas_semana(ws) if periodo == "semana" else get_filas_hoy(ws)
    except Exception as e:
        print(f"[/analizar] ERROR leyendo Sheets: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": f"Error accediendo a Google Sheets: {e}"}), 500

    if not filas:
        label = "esta semana" if periodo == "semana" else "hoy"
        return jsonify({"error": f"No hay contexto acumulado {label}."}), 404

    limite = 200 if periodo == "semana" else 50
    if len(filas) > limite:
        print(f"[/analizar] {len(filas)} fragmentos — limitando a {limite}", flush=True)
        filas = filas[-limite:]

    n             = len(filas)
    contexto_texto = "\n".join(f"[{f[0]}] {f[1]}" for f in filas)
    print(f"[/analizar] Analizando {n} fragmentos en background (periodo={periodo})", flush=True)

    if periodo == "semana":
        informe_anterior = get_ultimo_informe_semanal()
        system_prompt = WEEKLY_SYSTEM_PROMPT if informe_anterior else WEEKLY_SYSTEM_PROMPT_PRIMERA_SEMANA
    else:
        informe_anterior = None
        system_prompt = construir_system_prompt(parse_perfil(request.args.get("perfil")))

    threading.Thread(
        target=_analizar_en_background,
        args=(contexto_texto, system_prompt, n, periodo, informe_anterior),
        daemon=True
    ).start()

    return jsonify({"status": "procesando", "fragmentos": n, "periodo": periodo})


@app.route("/contexto", methods=["POST"])
def agregar_contexto():
    """Inserta una transcripción directamente en Sheets (útil para tests y simulación)."""
    data      = request.get_json(silent=True) or {}
    texto     = data.get("texto", "").strip()
    timestamp = data.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not texto:
        return jsonify({"error": "Falta el campo 'texto'."}), 400
    try:
        ws = get_sheet()
        ws.append_row([timestamp, texto])
        print(f"[/contexto POST] Insertado: [{timestamp}] {texto[:60]}", flush=True)
        return jsonify({"ok": True, "timestamp": timestamp})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
            max_tokens=2048,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": DECISIONES_USER_PROMPT.format(conversaciones=texto)
            }]
        )
        decisiones = respuesta.content[0].text
        with open(DECISIONES_FILE, "w", encoding="utf-8") as f:
            f.write(decisiones)
        return jsonify({"texto": texto, "decisiones": decisiones})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/limpiar-sheets", methods=["POST"])
def limpiar_sheets():
    """Borra todas las filas de datos de Sheet1 y decisiones_semana, dejando solo encabezados."""
    try:
        client = _gspread_client()
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

        ws1 = spreadsheet.sheet1
        todas1 = ws1.get_all_values()
        if len(todas1) > 1:
            ws1.delete_rows(2, len(todas1))
        n1 = len(todas1) - 1

        try:
            ws2 = spreadsheet.worksheet("decisiones_semana")
            todas2 = ws2.get_all_values()
            if len(todas2) > 1:
                ws2.delete_rows(2, len(todas2))
            n2 = len(todas2) - 1
        except gspread.exceptions.WorksheetNotFound:
            n2 = 0

        print(f"[/admin/limpiar-sheets] Sheet1: {n1} filas borradas, decisiones_semana: {n2} filas borradas", flush=True)
        return jsonify({"ok": True, "sheet1_borradas": n1, "decisiones_semana_borradas": n2})
    except Exception as e:
        print(f"[/admin/limpiar-sheets] ERROR: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/decisiones", methods=["GET"])
def get_decisiones():
    periodo = request.args.get("periodo", "hoy")

    if periodo == "semana":
        try:
            ws    = get_decisiones_semana_sheet()
            todas = ws.get_all_values()
            datos = todas[1:] if len(todas) > 1 else []
            if not datos:
                return jsonify({"decisiones": None, "mensaje": "No hay informe semanal aún."}), 404
            ultimo = datos[-1]
            if len(ultimo) < 2 or not ultimo[1].strip():
                return jsonify({"decisiones": None, "mensaje": "No hay informe semanal aún."}), 404
            return jsonify({"decisiones": ultimo[1], "timestamp": ultimo[0]})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if not os.path.exists(DECISIONES_FILE):
        return jsonify({"decisiones": None, "mensaje": "No hay decisiones generadas aún."}), 404
    with open(DECISIONES_FILE, "r", encoding="utf-8") as f:
        contenido = f.read()
    return jsonify({"decisiones": contenido})
