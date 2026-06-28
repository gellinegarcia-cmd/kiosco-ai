import os
import json
import time
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

_cache = {}
CACHE_TTL = 60  # segundos

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry['ts'] < CACHE_TTL:
        return entry['val']
    return None

def cache_set(key, val):
    _cache[key] = {'val': val, 'ts': time.time()}

def cache_del(key):
    _cache.pop(key, None)

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
- Generá EXACTAMENTE 3 decisiones: una urgente, una importante, una de reflexión — nunca más de una por categoría
- CORRECCIÓN DE TRANSCRIPCIÓN: las conversaciones vienen de audio transcripto automáticamente y pueden tener errores. Si una palabra no tiene sentido como producto o término del rubro de este negocio, pero suena parecida (fonéticamente) a un producto o término que SÍ es típico de ese rubro, asumí que es un error de transcripción y usá el término correcto en tu análisis. Inferí el rubro directamente del contexto de las conversaciones — nunca lo pidas al usuario, nunca te detengas a confirmar. Generá el análisis completo siempre.
- NUNCA hagas preguntas ni pidas confirmación al usuario. Siempre asumí el rubro y contexto del negocio a partir de las conversaciones disponibles y generá el análisis completo en el formato pedido, sin excepciones."""

DECISIONES_USER_PROMPT = """\
Analizá las conversaciones del negocio y generá exactamente este formato, sin texto antes ni después:

### Plata que se te escapa hoy

[UNA SOLA FRASE corta tipo titular de diario, máximo 20 palabras. Tiene que doler. Ejemplo de estilo: "Hoy se fueron dos ventas por la puerta: el cliente quería A4 y cidrex de colores, y no tenías ninguno de los dos."]

**Hacé esto:** [UNA orden directa con verbo imperativo, máximo 15 palabras. Ejemplo: "Llamá al proveedor ahora y sumá A4 y cidrex de colores al pedido del miércoles."]

### Esto se viene repitiendo

[UNA SOLA FRASE corta tipo titular, máximo 20 palabras, con la evidencia de que se repite.]

**Hacé esto:** [UNA orden directa, máximo 15 palabras.]

### Para que no te vuelva a pasar

[UNA SOLA FRASE corta tipo titular, máximo 20 palabras, sobre la causa estructural.]

**Hacé esto:** [UNA orden o sugerencia directa, máximo 15 palabras.]

### Lo que funcionó

[UNA SOLA FRASE corta, máximo 20 palabras, reconociendo algo concreto que salió bien. Si no hay nada concreto, omitir TODA la sección incluyendo el título.]

REGLAS ESTRICTAS:
- Cada frase es UNA oración, no un párrafo. Si tenés mucha información, elegí lo más importante y descartá el resto.
- "Hacé esto:" es una ORDEN, no una explicación — empieza con verbo (Llamá, Revisá, Armá, Pedí, etc.)
- Sin emojis
- Exactamente esos 3 títulos en ese orden, más "Lo que funcionó" si aplica
- Línea vacía real entre el titular y "Hacé esto:"

CONVERSACIONES:
{conversaciones}"""

WEEKLY_SYSTEM_PROMPT = """\
Sos Gelline, el socio silencioso de este negocio argentino. Hablás en español rioplatense, directo y cálido, sin jerga técnica. Sos el mismo Gelline del resumen diario.

Tu trabajo es detectar CAMBIOS respecto a patrones anteriores — no repetir información ya conocida. Si algo siempre fue igual, no lo menciones. Solo importa lo que es diferente.

Ignorá conversaciones que no son del negocio (charlas personales, TV, ruido).

CORRECCIÓN DE TRANSCRIPCIÓN: las conversaciones vienen de audio transcripto automáticamente y pueden tener errores. Si una palabra no tiene sentido como producto o término del rubro de este negocio, pero suena parecida (fonéticamente) a un producto o término que SÍ es típico de ese rubro, asumí que es un error de transcripción y usá el término correcto en tu análisis. Inferí el rubro directamente del contexto de las conversaciones — nunca lo pidas al usuario, nunca te detengas a confirmar. Generá el análisis completo siempre.

FORMATO OBLIGATORIO — respondé ÚNICAMENTE con este formato, sin texto antes ni después:

## LO QUE CAMBIÓ
3 a 5 cosas que cambiaron esta semana respecto a lo normal: productos que dejaron de pedirse, quejas nuevas, caídas o subas de actividad en algún horario, algo que antes funcionaba y ahora no. Si tenés el informe de la semana anterior, compará explícitamente.

## LO QUE SE MANTIENE FUERTE
3 a 5 cosas que siguen funcionando bien, igual o mejor que antes. Lo que no hay que tocar.

## 3 ACCIONES PARA ESTA SEMANA

### 1. [Titular corto, máximo 10 palabras, que resume el problema o la oportunidad]

[UNA SOLA FRASE tipo titular, máximo 20 palabras, con la evidencia concreta de esta semana que justifica la acción.]

**Hacé esto:** [UNA orden directa con verbo imperativo, máximo 15 palabras, con cuándo.]

### 2. [Titular corto, máximo 10 palabras]

[UNA SOLA FRASE tipo titular, máximo 20 palabras, con evidencia concreta.]

**Hacé esto:** [UNA orden directa con verbo imperativo, máximo 15 palabras, con cuándo.]

### 3. [Titular corto, máximo 10 palabras]

[UNA SOLA FRASE tipo titular, máximo 20 palabras, con evidencia concreta.]

**Hacé esto:** [UNA orden directa con verbo imperativo, máximo 15 palabras, con cuándo.]

NUNCA hagas preguntas ni pidas confirmación al usuario. Siempre asumí el rubro y contexto del negocio a partir de las conversaciones disponibles y generá el análisis completo en el formato pedido, sin excepciones."""

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

DIAS_SEMANA = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

HORARIOS_HEADERS = ["dia", "tipo", "cerrado", "apertura", "cierre", "siesta", "siesta_inicio", "siesta_fin"]

HORARIOS_DEFAULTS = [
    {"dia": d, "tipo": "general", "cerrado": "false",
     "apertura": "09:00", "cierre": "20:00",
     "siesta": "false", "siesta_inicio": "13:00", "siesta_fin": "14:00"}
    for d in DIAS_SEMANA
]


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


def get_horarios_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("horarios_semana")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="horarios_semana", rows=10, cols=len(HORARIOS_HEADERS))
        ws.append_row(HORARIOS_HEADERS)
        for d in HORARIOS_DEFAULTS:
            ws.append_row([d[h] for h in HORARIOS_HEADERS])
    return ws


def leer_horarios(ws):
    todas = ws.get_all_values()
    if len(todas) < 2:
        return {d["dia"]: d for d in HORARIOS_DEFAULTS}
    headers = todas[0]
    result = {}
    for fila in todas[1:]:
        row = {headers[i]: (fila[i] if i < len(fila) else "") for i in range(len(headers))}
        result[row.get("dia", "")] = row
    return result


def get_dia_hoy():
    tz_ar = timezone(timedelta(hours=-3))
    idx = datetime.now(tz_ar).weekday()
    return DIAS_SEMANA[idx]


def leer_config(ws):
    """Lee la fila de valores de la hoja config y devuelve un dict."""
    cached = cache_get('config')
    if cached:
        return cached
    todas = ws.get_all_values()
    if len(todas) < 2:
        return dict(CONFIG_DEFAULTS)
    headers = todas[0]
    valores = todas[1]
    result = {h: (valores[i] if i < len(valores) else CONFIG_DEFAULTS.get(h, ""))
              for i, h in enumerate(headers)}
    cache_set('config', result)
    return result


def calcular_debe_grabar(cfg, horarios=None):
    if cfg.get("activo", "false").lower() != "true":
        return False
    tz_ar = timezone(timedelta(hours=-3))
    ahora = datetime.now(tz_ar)
    cur = ahora.hour * 60 + ahora.minute
    def hm(t):
        try:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 0
    horario_hoy = None
    if horarios:
        dia_hoy = get_dia_hoy()
        entrada = horarios.get(dia_hoy, {})
        if entrada.get("tipo", "general") != "general":
            horario_hoy = entrada
    if horario_hoy:
        if horario_hoy.get("cerrado", "false").lower() == "true":
            return False
        if not (hm(horario_hoy.get("apertura", "09:00")) <= cur < hm(horario_hoy.get("cierre", "20:00"))):
            return False
        if horario_hoy.get("siesta", "false").lower() == "true":
            if hm(horario_hoy.get("siesta_inicio", "13:00")) <= cur < hm(horario_hoy.get("siesta_fin", "14:00")):
                return False
    else:
        if not (hm(cfg.get("hora_apertura", "09:00")) <= cur < hm(cfg.get("hora_cierre", "20:00"))):
            return False
        if cfg.get("descanso", "false").lower() == "true":
            if hm(cfg.get("hora_siesta_inicio", "13:00")) <= cur < hm(cfg.get("hora_siesta_fin", "14:00")):
                return False
    return True


def get_filas_hoy(ws):
    cached = cache_get('filas_hoy')
    if cached is not None:
        return cached
    hoy = date.today().isoformat()
    todas = ws.get_all_values()
    datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
    result = [f for f in datos if len(f) >= 2 and f[0].startswith(hoy)]
    cache_set('filas_hoy', result)
    return result


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
        "debe_grabar":       calcular_debe_grabar(cfg, leer_horarios(get_horarios_sheet())),
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
        cache_del('config')
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
            cache_del('filas_hoy')
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


@app.route("/horarios", methods=["GET"])
def get_horarios():
    try:
        ws = get_horarios_sheet()
        horarios = leer_horarios(ws)
        return jsonify({"horarios": list(horarios.values())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/horarios", methods=["POST"])
def set_horarios():
    data = request.get_json(silent=True) or {}
    dias_data = data.get("horarios", [])
    if not dias_data:
        return jsonify({"error": "Falta el campo 'horarios'."}), 400
    def bool_str(v):
        return "true" if (v is True or str(v).lower() == "true") else "false"
    try:
        ws = get_horarios_sheet()
        todas = ws.get_all_values()
        headers = todas[0] if todas else HORARIOS_HEADERS
        for dia_data in dias_data:
            dia = dia_data.get("dia", "").lower()
            if dia not in DIAS_SEMANA:
                continue
            nueva_fila = [
                dia,
                dia_data.get("tipo", "general"),
                bool_str(dia_data.get("cerrado", False)),
                dia_data.get("apertura", "09:00"),
                dia_data.get("cierre", "20:00"),
                bool_str(dia_data.get("siesta", False)),
                dia_data.get("siesta_inicio", "13:00"),
                dia_data.get("siesta_fin", "14:00"),
            ]
            fila_idx = None
            for i, fila in enumerate(todas):
                if i > 0 and fila and fila[0] == dia:
                    fila_idx = i + 1
                    break
            if fila_idx:
                col_fin = chr(ord("A") + len(headers) - 1)
                ws.update(f"A{fila_idx}:{col_fin}{fila_idx}", [nueva_fila])
            else:
                ws.append_row(nueva_fila)
        print(f"[/horarios POST] Guardados {len(dias_data)} días", flush=True)
        ws2 = get_horarios_sheet()
        return jsonify({"ok": True, "horarios": list(leer_horarios(ws2).values())})
    except Exception as e:
        print(f"[/horarios POST] ERROR: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/admin/status", methods=["GET"])
def admin_status():
    try:
        ws_config = get_config_sheet()
        cfg = leer_config(ws_config)
        ws_horarios = get_horarios_sheet()
        horarios = leer_horarios(ws_horarios)
        debe_grabar = calcular_debe_grabar(cfg, horarios)

        tz_ar = timezone(timedelta(hours=-3))
        ahora = datetime.now(tz_ar)
        dia_hoy = get_dia_hoy()
        horario_hoy = horarios.get(dia_hoy, {})

        ws = get_sheet()
        filas_hoy = get_filas_hoy(ws)
        total_chars = sum(len(f[1]) for f in filas_hoy if len(f) >= 2)

        primer_audio = filas_hoy[0][0] if filas_hoy else None
        ultimo_audio = filas_hoy[-1][0] if filas_hoy else None

        return jsonify({
            "timestamp": ahora.strftime("%Y-%m-%d %H:%M:%S"),
            "debe_grabar": debe_grabar,
            "activo": cfg.get("activo", "false").lower() == "true",
            "dia_hoy": dia_hoy,
            "horario_hoy": horario_hoy,
            "fragmentos_hoy": len(filas_hoy),
            "chars_hoy": total_chars,
            "primer_audio": primer_audio,
            "ultimo_audio": ultimo_audio,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/transcripciones", methods=["GET"])
def admin_transcripciones():
    try:
        ws = get_sheet()
        filas = get_filas_hoy(ws)
        result = []
        for f in filas:
            if len(f) >= 2:
                result.append({
                    "timestamp": f[0],
                    "texto": f[1],
                    "chars": len(f[1]),
                })
        return jsonify({"transcripciones": result, "total": len(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/heartbeat", methods=["POST"])
def admin_heartbeat():
    data = request.get_json(silent=True) or {}
    bateria = data.get("bateria", 0)
    estado = data.get("estado", "desconocido")
    tz_ar = timezone(timedelta(hours=-3))
    ts = datetime.now(tz_ar).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[heartbeat] {ts} · batería {bateria}% · estado: {estado}", flush=True)
    return jsonify({"ok": True, "timestamp": ts})
