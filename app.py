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
GOOGLE_SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
PERSONAL_SHEET_ID = os.environ.get("PERSONAL_SHEET_ID", "1T874S-Qew1SESjyT7Z5kpRI-3_a0IOu2WoWusaV04Y8")
POSTA_SHEET_ID    = os.environ.get("POSTA_SHEET_ID",    "1uAMt18X-eN-cEk-q_XlWpMx6gFSCDaGKw37WEuvO3N4")
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

POSTA_SYSTEM_PROMPT = """\
Sos un equipo de médicos intensivistas especializados — neurocrítico, infectólogo, neumólogo, nefrólogo, hemodinamista — parados al lado de la cama de un paciente de UTI/UCI, escuchando el pase oral del médico de guardia.

Tu respuesta tiene DOS partes separadas por una línea divisoria. Nada más, nada menos.

═══════════════════════════════════
PARTE 1 — EVOLUCIÓN
═══════════════════════════════════
Escribís la evolución del día exactamente como lo haría un terapista en el parte de guardia.

REGLA ABSOLUTA: Solo escribís lo que el médico dijo. Si no lo mencionó, no va. Sin inferencias, sin completar, sin "dosis no especificada", sin aclarar lo que falta.

ESTILO:
- Texto corrido, sin títulos, sin bullets, sin asteriscos
- Orden: neurológico → hemodinámico → ventilatorio → renal → infeccioso → metabólico/nutricional → procedimientos del día → plan
- Solo los sistemas mencionados
- Números concretos tal como los dijo el médico: dosis, PAFI, PEEP, Glasgow, RASS, ml/hr, gamas
- Abreviaciones estándar: ARM, PVE, TT, NET, NPT, TRR, ATB, NAD, IOT, TQT, DVE, VCV, PCV, BNM, etc.
- Sin sección de pendientes separada — si el médico mencionó algo pendiente, aparece en el texto corrido donde corresponde
- Si hay error de Whisper, interpretá con criterio clínico

COMANDOS NATURALES A RECONOCER:
- "agregame / sumame / incluí [dato]" → incorporás donde corresponde
- "borrá / sacá [dato]" → lo eliminás
- "eso es todo / listo / fin" → cerrás la evolución

EJEMPLO DE EVOLUCIÓN ESPERADA:
sedoanalgesiado, RASS -5, midazolam + fentanilo + atracurio. pupilas mióticas reactivas. DVE funcionante, 20ml en el turno, curva de buena complacencia cerebral. TC control en la tarde.
requerimiento de vasopresores, noradrenalina 0,08 mcg/kg/min en descenso.
ARM VCV, FiO2 50%, PEEP 10, PAFI 220. moderada movilización de secreciones.
adecuado ritmo diurético.
afebril, tercer día de meropenem a foco respiratorio.

---ANALISIS POSTA---

═══════════════════════════════════
PARTE 2 — ANÁLISIS POSTA
═══════════════════════════════════
Sos el equipo especialista. Analizás la evolución completa del paciente — diagnóstico, historia, lo que se dijo hoy y lo que NO se dijo — y hacés observaciones clínicas relevantes.

FUENTES QUE SUSTENTAN CADA AFIRMACIÓN:
Cada observación que hacés está respaldada internamente por la evidencia más robusta y actualizada de terapia intensiva:
- Surviving Sepsis Campaign (SSC) — últimas guías
- European Society of Intensive Care Medicine (ESICM)
- Society of Critical Care Medicine (SCCM)
- Neurocritical Care Society (NCS)
- NEJM, Lancet, Critical Care Medicine, Intensive Care Medicine, JAMA, CHEST
- ARDS Network, LAS VEGAS study, PROVE Network
- Si no tenés evidencia robusta para una afirmación → no la hacés

REGLAS DEL ANÁLISIS:
- Solo observaciones con relevancia clínica real para ESTE paciente en ESTE momento
- No repetís lo que ya está en la evolución
- No hacés preguntas — afirmás con criterio clínico especializado
- Máximo 5-6 puntos. Si hay menos puntos relevantes → menos puntos. Calidad sobre cantidad
- Cada punto: una línea, directo, accionable
- Sin obviedad, sin relleno, sin CYA medicine
- Considerás: lo que se mencionó + lo que NO se mencionó + el diagnóstico de base + días de evolución + medicación activa + cultivos + procedimientos

EJEMPLOS DEL ESTILO ESPERADO:
· Posquirúrgico de subdural → cabecera a 30° mínimo, verificar posicionamiento.
· DVE con 20ml en el turno → confirmar nivel del sistema y presión de cierre documentada.
· Atracurio activo con más de 48hs → vigilar fuerza muscular al retirar BNM, riesgo de miopatía del crítico aumenta con corticoides concomitantes.
· Sin mención de profilaxis de TVP → confirmar HBPM o documentar contraindicación activa.
· Tercer día de meropenem sin mención de cultivos enviados → considerar enviar muestra si no hay rescate previo.
· PAFI en descenso progresivo últimas 24hs → evaluar criterios de prono si llega a menos de 150.

LO QUE NUNCA HACÉS:
- Repetir información de la evolución
- Hacer observaciones sin relevancia para el paciente actual
- Inventar datos que no están en el contexto
- Dar más de 6 puntos
- Usar lenguaje vago o genérico
"""

POSTA_CONSULTOR_PROMPT = """\
Sos POSTA, el asistente clínico de guardia. Tu rol en este momento es ser un colega inteligente que conoce al paciente y responde preguntas clínicas de forma directa, precisa y cercana.

COMPORTAMIENTO:
- Respondé como un médico intensivista experimentado hablando con un colega
- Usá el contexto completo del paciente para dar respuestas específicas, no genéricas
- Si hay datos en la historia clínica que son relevantes para la pregunta, citálos
- Sé conciso pero completo — ni muy corto ni innecesariamente largo
- Si no tenés suficiente información para responder con certeza, decilo claramente
- No generés evoluciones ni documentos — solo respondé la pregunta
- Usá lenguaje clínico pero accesible
- En español rioplatense, directo y sin rodeos

NUNCA:
- Generés secciones con ### o formato de evolución
- Respondas con bullets innecesarios si una frase alcanza
- Inventés datos que no están en el contexto
- Digas "como IA" o "como asistente" """

JAVI_PROCESADOR_PROMPT = """\
Sos JAVI, el compañero de guardia de un médico. Escuchaste un fragmento de audio de su guardia y tenés que procesarlo.

Tu trabajo tiene DOS partes:

PARTE 1 — ATRIBUCIÓN Y EVOLUCIÓN
Identificá a qué paciente pertenece el fragmento. El médico puede referirse por:
- Número de cama: "la cama 8", "el de la tres"
- Diagnóstico: "el de la HSA", "el quemado", "el del ACV"
- Nombre: "Pérez", "la señora González"
- Número de orden: "el siguiente", "el próximo"

Si podés identificar al paciente → actualizá su evolución con lo que se dijo.
Si NO podés identificar → marcá como "sin asignar" con el fragmento exacto.

PARTE 2 — DETECCIÓN DE PENDIENTES Y ALERTAS
Del fragmento detectá:
- Pendientes con hora: "repetir gas en la noche", "TAC control en la tarde"
- Alertas clínicas: situaciones que requieren atención
- Mensaje para el médico si hay algo importante

RESPONDÉ SOLO con este JSON, sin texto adicional:
{
  "pacientes_detectados": [
    {
      "id": "identificador único basado en cama o nombre",
      "cama": "número de cama o null",
      "nombre": "nombre del paciente o descripción",
      "dx": "diagnóstico principal",
      "estado": "estable|critico|pendiente",
      "evolucion_fragmento": "lo que se dijo de este paciente"
    }
  ],
  "sin_asignar": "fragmento que no pudo asignarse o null",
  "mensaje_javi": "mensaje importante para el médico o null",
  "tipo_mensaje": "clinico|importante|humano",
  "nuevos_pendientes": 0,
  "nuevas_alertas": 0
}
"""

JAVI_CONSULTOR_PROMPT = """\
Sos JAVI, el compañero de guardia de un médico intensivista. El médico te hace una pregunta clínica en tiempo real durante su guardia.

Respondés como un especialista que está parado al lado de la cama:
- Directo, preciso, sin vueltas
- Basado en evidencia de las guías más actuales (SSC, ESICM, SCCM, NCS, NEJM, Lancet, Critical Care Medicine)
- En español rioplatense médico
- Máximo 3-4 líneas — el médico está en guardia, no tiene tiempo
- Si la pregunta involucra ajuste a función renal, pedís el clearance si no lo tenés, o calculás con el dato disponible
- Nunca decís "como IA" ni "no soy médico"
- Si no sabés algo con certeza → decilo directo

Ejemplos de lo que respondés:
"dosis de colistin ajustada a función renal con clearance 30" → dosis exacta con intervalo
"criterios de prono" → criterios SSC actualizados en 2 líneas
"cuándo extubás a un paciente post HSA" → criterios clínicos concretos
"""



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


def get_personal_sheet():
    """Devuelve la hoja principal del Sheet personal de Gelline."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(PERSONAL_SHEET_ID)
    ws = spreadsheet.sheet1
    if not ws.get_all_values():
        ws.append_row(["timestamp", "transcripcion"])
    return ws

def get_personal_config_sheet():
    """Devuelve la hoja config del Sheet personal, creándola si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(PERSONAL_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("config")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="config", rows=2, cols=len(CONFIG_HEADERS))
        ws.append_row(CONFIG_HEADERS)
        ws.append_row([CONFIG_DEFAULTS[h] for h in CONFIG_HEADERS])
    return ws

def get_personal_horarios_sheet():
    """Devuelve la hoja horarios_semana del Sheet personal, creándola si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(PERSONAL_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("horarios_semana")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="horarios_semana", rows=10, cols=len(HORARIOS_HEADERS))
        ws.append_row(HORARIOS_HEADERS)
        for d in HORARIOS_DEFAULTS:
            ws.append_row([d[h] for h in HORARIOS_HEADERS])
    return ws

def get_personal_filas_hoy(ws):
    """Devuelve las filas de hoy del Sheet personal."""
    cached = cache_get('personal_filas_hoy')
    if cached is not None:
        return cached
    hoy = date.today().isoformat()
    todas = ws.get_all_values()
    datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
    result = [f for f in datos if len(f) >= 2 and f[0].startswith(hoy)]
    cache_set('personal_filas_hoy', result)
    return result

def get_posta_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    ws = spreadsheet.sheet1
    if not ws.get_all_values():
        ws.append_row(["timestamp", "turno_id", "cama", "nombre", "dni", "edad", "dx", "transcripcion", "rol"])
    return ws

def get_posta_config_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("config")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="config", rows=2, cols=len(CONFIG_HEADERS))
        ws.append_row(CONFIG_HEADERS)
        ws.append_row([CONFIG_DEFAULTS[h] for h in CONFIG_HEADERS])
    return ws

def get_posta_turnos_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("turnos")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="turnos", rows=500, cols=6)
        ws.append_row(["turno_id", "timestamp", "rol", "medico", "pacientes_json", "pdf_texto"])
    return ws

def get_posta_pacientes_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("pacientes_activos")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="pacientes_activos", rows=500, cols=12)
        ws.append_row(["id_paciente", "servicio_id", "cama", "nombre", "dni", "edad", "dx", "institucion", "fecha_ingreso", "activo", "fecha_egreso", "notas"])
    return ws

def get_posta_evoluciones_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("evoluciones")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="evoluciones", rows=2000, cols=10)
        ws.append_row(["id_paciente", "fecha", "turno", "rol", "medico", "matricula", "servicio_id", "evolucion_completa", "turno_id", "institucion"])
    return ws

def get_posta_alertas_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("alertas")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="alertas", rows=2000, cols=10)
        ws.append_row(["id", "id_paciente", "servicio_id", "texto", "tipo", "fecha_creacion", "fecha_recordatorio", "cumplida", "medico", "turno_id"])
    return ws

def get_posta_resumenes_sheet():
    client = _gspread_client()
    spreadsheet = client.open_by_key(POSTA_SHEET_ID)
    try:
        ws = spreadsheet.worksheet("resumenes")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="resumenes", rows=1000, cols=6)
        ws.append_row(["id_paciente", "fecha", "resumen", "ultima_actualizacion", "servicio_id", "medico"])
    return ws

def generar_id_paciente(servicio_id, cama):
    import hashlib
    base = f"{servicio_id}-{cama}-{datetime.now().strftime('%Y%m%d')}"
    return "P" + hashlib.md5(base.encode()).hexdigest()[:8].upper()

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


def get_memoria_sheet():
    """Devuelve la hoja 'memoria_gelline', creándola si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        return spreadsheet.worksheet("memoria_gelline")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="memoria_gelline", rows=100, cols=3)
        ws.append_row(["semana", "timestamp", "memoria"])
        return ws

def get_chat_contador_sheet():
    """Devuelve la hoja 'chat_contador', creándola si no existe."""
    client = _gspread_client()
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        return spreadsheet.worksheet("chat_contador")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="chat_contador", rows=100, cols=3)
        ws.append_row(["fecha", "contador", "historial"])
        return ws

def get_memoria_acumulada():
    """Lee todas las memorias semanales y las concatena."""
    try:
        ws = get_memoria_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        if not datos:
            return None
        return "\n\n---\n\n".join(
            f"SEMANA {d[0]}:\n{d[2]}" for d in datos if len(d) >= 3 and d[2].strip()
        )
    except Exception as e:
        print(f"[get_memoria_acumulada] ERROR: {e}", flush=True)
        return None

def get_chat_hoy():
    """Devuelve el contador y historial de chat de hoy."""
    try:
        ws = get_chat_contador_sheet()
        todas = ws.get_all_values()
        hoy = date.today().isoformat()
        datos = todas[1:] if len(todas) > 1 else []
        for fila in reversed(datos):
            if len(fila) >= 2 and fila[0] == hoy:
                contador = int(fila[1]) if fila[1].isdigit() else 0
                historial = json.loads(fila[2]) if len(fila) >= 3 and fila[2] else []
                return ws, contador, historial, True
        return ws, 0, [], False
    except Exception as e:
        print(f"[get_chat_hoy] ERROR: {e}", flush=True)
        return None, 0, [], False

def guardar_chat_hoy(ws, contador, historial, existe):
    """Guarda o actualiza el contador y historial de chat de hoy."""
    try:
        hoy = date.today().isoformat()
        fila = [hoy, str(contador), json.dumps(historial, ensure_ascii=False)]
        if existe:
            todas = ws.get_all_values()
            for i, f in enumerate(todas):
                if i > 0 and len(f) >= 1 and f[0] == hoy:
                    ws.update(f"A{i+1}:C{i+1}", [fila])
                    return
        ws.append_row(fila)
    except Exception as e:
        print(f"[guardar_chat_hoy] ERROR: {e}", flush=True)

CHAT_SYSTEM_PROMPT = """\
Sos Gelline, el socio silencioso de este negocio argentino. Llevás semanas escuchando lo que pasa en el local y conocés el negocio mejor que nadie desde adentro.

Cuando el dueño te hace una pregunta, respondés como un socio de confianza que conoce la historia del negocio — conectás lo que escuchaste hoy con lo que aprendiste las semanas anteriores. Usás ejemplos concretos de conversaciones reales cuando los tenés.

REGLAS:
- Hablás en español rioplatense, directo y cálido
- Máximo 3 párrafos cortos por respuesta
- Nunca decís "según mis datos" ni "basándome en" — simplemente contás lo que sabés como alguien que estuvo ahí
- Si no tenés información sobre algo, lo decís honestamente y sugerís cómo conseguirla
- Conectás información de distintas semanas cuando es relevante
- Nunca hacés preguntas al final de tu respuesta — cerrás con una observación o sugerencia concreta
- CORRECCIÓN DE TRANSCRIPCIÓN: las conversaciones vienen de audio transcripto automáticamente y pueden tener errores fonéticos — interpretá el contexto correctamente"""


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


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    pregunta = data.get("pregunta", "").strip()
    if not pregunta:
        return jsonify({"error": "Falta el campo 'pregunta'."}), 400
    if len(pregunta) > 300:
        return jsonify({"error": "Pregunta demasiado larga. Máximo 300 caracteres."}), 400

    ws_chat, contador, historial, existe = get_chat_hoy()
    LIMITE_DIARIO = 10
    if contador >= LIMITE_DIARIO:
        return jsonify({
            "error": "limite_alcanzado",
            "mensaje": f"Usaste las {LIMITE_DIARIO} consultas de hoy. Mañana podés seguir preguntando.",
            "contador": contador,
            "limite": LIMITE_DIARIO,
        }), 429

    try:
        ws = get_sheet()
        filas_hoy = get_filas_hoy(ws)
        contexto_hoy = "\n".join(f"[{f[0]}] {f[1]}" for f in filas_hoy) if filas_hoy else "Sin conversaciones registradas hoy todavía."

        memoria = get_memoria_acumulada()
        contexto_memoria = f"MEMORIA ACUMULADA DEL NEGOCIO:\n{memoria}\n\n" if memoria else ""

        contexto_completo = f"{contexto_memoria}CONVERSACIONES DE HOY:\n{contexto_hoy}"

        messages = []
        for msg in historial[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({
            "role": "user",
            "content": f"CONTEXTO DEL NEGOCIO:\n{contexto_completo}\n\nPREGUNTA DEL DUEÑO:\n{pregunta}"
        })

        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=CHAT_SYSTEM_PROMPT,
            messages=messages,
        )
        texto_respuesta = respuesta.content[0].text

        historial.append({"role": "user", "content": pregunta})
        historial.append({"role": "assistant", "content": texto_respuesta})
        nuevo_contador = contador + 1
        guardar_chat_hoy(ws_chat, nuevo_contador, historial, existe)
        cache_del('filas_hoy')

        print(f"[/chat] Pregunta procesada — consulta {nuevo_contador}/{LIMITE_DIARIO}", flush=True)
        return jsonify({
            "respuesta": texto_respuesta,
            "contador": nuevo_contador,
            "limite": LIMITE_DIARIO,
            "restantes": LIMITE_DIARIO - nuevo_contador,
        })
    except Exception as e:
        print(f"[/chat] ERROR: {type(e).__name__}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/memoria/generar", methods=["POST"])
def generar_memoria():
    """Genera y guarda el resumen de memoria de la semana actual."""
    print("[/memoria/generar] Generando memoria semanal...", flush=True)
    try:
        ws = get_sheet()
        filas = get_filas_semana(ws)
        if not filas:
            return jsonify({"error": "No hay transcripciones esta semana para generar memoria."}), 404

        contexto = "\n".join(f"[{f[0]}] {f[1]}" for f in filas)
        memoria_anterior = get_memoria_acumulada()

        system = """\
Sos Gelline analizando las conversaciones de la semana para construir tu memoria interna del negocio.
Tu objetivo es extraer conocimiento acumulable — no decisiones puntuales sino patrones, preferencias de clientes,
productos estrella, problemas recurrentes, oportunidades, contexto del negocio.
Esta memoria la vas a usar para responder preguntas del dueño con contexto histórico.
Escribí en primera persona, como si fuera tu diario de aprendizaje sobre este negocio.
Sé específico — mencioná productos, precios, nombres si los hay, situaciones concretas.
Máximo 400 palabras."""

        user_content = ""
        if memoria_anterior:
            user_content = f"MEMORIA ACUMULADA HASTA AHORA:\n{memoria_anterior}\n\nCONVERSACIONES DE ESTA SEMANA:\n{contexto}\n\nActualizá y enriquecé la memoria con lo nuevo que aprendiste esta semana."
        else:
            user_content = f"CONVERSACIONES DE ESTA SEMANA:\n{contexto}\n\nEsta es la primera semana. Construí la memoria inicial del negocio."

        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": user_content}]
        )
        memoria_nueva = respuesta.content[0].text

        ws_mem = get_memoria_sheet()
        semana = date.today().strftime("%Y-W%W")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws_mem.append_row([semana, ts, memoria_nueva])

        print(f"[/memoria/generar] Memoria generada para semana {semana}", flush=True)
        return jsonify({"ok": True, "semana": semana, "memoria": memoria_nueva})
    except Exception as e:
        print(f"[/memoria/generar] ERROR: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


@app.route("/memoria", methods=["GET"])
def ver_memoria():
    """Devuelve toda la memoria acumulada."""
    try:
        ws = get_memoria_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        return jsonify({
            "semanas": len(datos),
            "memoria": [{"semana": d[0], "timestamp": d[1], "contenido": d[2]} for d in datos if len(d) >= 3]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Endpoints personales (Sheet de Gelline) ────────────────────────────────────

@app.route("/personal/config", methods=["GET"])
def get_personal_config():
    try:
        ws = get_personal_config_sheet()
        cfg = leer_config(ws)
        ws_h = get_personal_horarios_sheet()
        horarios = leer_horarios(ws_h)
        debe_grabar = calcular_debe_grabar(cfg, horarios)
        def b(k): return cfg.get(k, "false").lower() == "true"
        return jsonify({
            "activo":            b("activo"),
            "apertura":          cfg.get("hora_apertura",      "07:00"),
            "cierre":            cfg.get("hora_cierre",        "21:00"),
            "descanso":          b("descanso"),
            "descanso_inicio":   cfg.get("hora_siesta_inicio", "13:00"),
            "descanso_fin":      cfg.get("hora_siesta_fin",    "15:00"),
            "saludo_automatico": b("saludo_automatico"),
            "debe_grabar":       debe_grabar,
            "es_dispositivo":    True,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/personal/config", methods=["POST"])
def set_personal_config():
    data = request.get_json(silent=True) or {}
    def bool_str(v):
        return "true" if (v is True or str(v).lower() == "true") else "false"
    nuevos = {
        "activo":             bool_str(data.get("activo",            False)),
        "hora_apertura":      data.get("apertura",          "07:00"),
        "hora_cierre":        data.get("cierre",            "21:00"),
        "descanso":           bool_str(data.get("descanso",          False)),
        "hora_siesta_inicio": data.get("descanso_inicio",   "13:00"),
        "hora_siesta_fin":    data.get("descanso_fin",      "15:00"),
        "saludo_automatico":  bool_str(data.get("saludo_automatico", False)),
    }
    try:
        ws = get_personal_config_sheet()
        todas = ws.get_all_values()
        if todas:
            headers = todas[0]
            fila = [nuevos.get(h, "") for h in headers]
            col_fin = chr(ord("A") + len(headers) - 1)
            if len(todas) >= 2:
                ws.update(f"A2:{col_fin}2", [fila])
            else:
                ws.append_row(fila)
        cache_del('config')
        def b(k): return nuevos[k] == "true"
        return jsonify({
            "activo":          b("activo"),
            "apertura":        nuevos["hora_apertura"],
            "cierre":          nuevos["hora_cierre"],
            "descanso":        b("descanso"),
            "descanso_inicio": nuevos["hora_siesta_inicio"],
            "descanso_fin":    nuevos["hora_siesta_fin"],
            "debe_grabar":     calcular_debe_grabar(nuevos, leer_horarios(get_personal_horarios_sheet())),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/personal/audio", methods=["POST"])
def procesar_personal_audio():
    print("[/personal/audio] Request recibido", flush=True)
    if "audio" not in request.files:
        return jsonify({"error": "No se recibió archivo de audio."}), 400
    archivo = request.files["audio"]
    sufijo = os.path.splitext(archivo.filename)[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        archivo.save(tmp.name)
        tmp_path = tmp.name
    tamanio_kb = os.path.getsize(tmp_path) / 1024
    if tamanio_kb < 1:
        os.remove(tmp_path)
        return jsonify({"error": "Archivo demasiado pequeño."}), 400
    try:
        openai_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        with open(tmp_path, "rb") as f:
            transcripcion = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="es"
            )
        texto = transcripcion.text.strip()
        if texto:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws = get_personal_sheet()
            ws.append_row([timestamp, texto])
            cache_del('personal_filas_hoy')
            print(f"[/personal/audio] Guardado: {texto[:80]}", flush=True)
        return jsonify({"transcripcion": texto, "acumulado": bool(texto)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.route("/personal/analizar", methods=["GET"])
def personal_analizar():
    print("[/personal/analizar] Request recibido", flush=True)
    try:
        ws = get_personal_sheet()
        filas = get_personal_filas_hoy(ws)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not filas:
        return jsonify({"error": "No hay contexto personal acumulado hoy."}), 404
    n = len(filas)
    contexto_texto = "\n".join(f"[{f[0]}] {f[1]}" for f in filas)
    threading.Thread(
        target=_analizar_en_background,
        args=(contexto_texto, BASE_SYSTEM_PROMPT, n, "personal", None),
        daemon=True
    ).start()
    return jsonify({"status": "procesando", "fragmentos": n})


@app.route("/personal/horarios", methods=["GET"])
def get_personal_horarios():
    try:
        ws = get_personal_horarios_sheet()
        horarios = leer_horarios(ws)
        return jsonify({"horarios": list(horarios.values())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/personal/horarios", methods=["POST"])
def set_personal_horarios():
    data = request.get_json(silent=True) or {}
    dias_data = data.get("horarios", [])
    if not dias_data:
        return jsonify({"error": "Falta el campo 'horarios'."}), 400
    def bool_str(v):
        return "true" if (v is True or str(v).lower() == "true") else "false"
    try:
        ws = get_personal_horarios_sheet()
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
                dia_data.get("apertura", "07:00"),
                dia_data.get("cierre", "21:00"),
                bool_str(dia_data.get("siesta", False)),
                dia_data.get("siesta_inicio", "13:00"),
                dia_data.get("siesta_fin", "15:00"),
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
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Endpoints POSTA ───────────────────────────────────────────────────────────

@app.route("/posta/config", methods=["GET"])
def posta_get_config():
    try:
        ws = get_posta_config_sheet()
        cfg = leer_config(ws)
        def b(k): return cfg.get(k, "false").lower() == "true"
        return jsonify({
            "activo": b("activo"),
            "apertura": cfg.get("hora_apertura", "07:00"),
            "cierre": cfg.get("hora_cierre", "21:00"),
            "debe_grabar": calcular_debe_grabar(cfg),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/config", methods=["POST"])
def posta_set_config():
    data = request.get_json(silent=True) or {}
    def bool_str(v): return "true" if (v is True or str(v).lower() == "true") else "false"
    nuevos = {
        "activo": bool_str(data.get("activo", False)),
        "hora_apertura": data.get("apertura", "07:00"),
        "hora_cierre": data.get("cierre", "21:00"),
        "descanso": "false",
        "hora_siesta_inicio": "13:00",
        "hora_siesta_fin": "14:00",
        "saludo_automatico": "false",
    }
    try:
        ws = get_posta_config_sheet()
        todas = ws.get_all_values()
        if todas:
            headers = todas[0]
            fila = [nuevos.get(h, "") for h in headers]
            col_fin = chr(ord("A") + len(headers) - 1)
            if len(todas) >= 2:
                ws.update(f"A2:{col_fin}2", [fila])
            else:
                ws.append_row(fila)
        cache_del('posta_config')
        return jsonify({"ok": True, "activo": nuevos["activo"] == "true"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/audio", methods=["POST"])
def posta_audio():
    if "audio" not in request.files:
        return jsonify({"error": "No se recibió audio."}), 400
    archivo = request.files["audio"]
    turno_id = request.form.get("turno_id", "")
    cama = request.form.get("cama", "")
    nombre = request.form.get("nombre", "")
    dni = request.form.get("dni", "")
    edad = request.form.get("edad", "")
    dx = request.form.get("dx", "")
    rol = request.form.get("rol", "medico")
    sufijo = os.path.splitext(archivo.filename)[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as tmp:
        archivo.save(tmp.name)
        tmp_path = tmp.name
    tamanio_kb = os.path.getsize(tmp_path) / 1024
    if tamanio_kb < 1:
        os.remove(tmp_path)
        return jsonify({"error": "Audio demasiado pequeño."}), 400
    try:
        openai_client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        with open(tmp_path, "rb") as f:
            transcripcion = openai_client.audio.transcriptions.create(
                model="whisper-1", file=f, language="es"
            )
        texto = transcripcion.text.strip()
        if texto:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ws = get_posta_sheet()
            ws.append_row([timestamp, turno_id, cama, nombre, dni, edad, dx, texto, rol])
            print(f"[/posta/audio] Guardado: cama {cama} · {len(texto)} chars", flush=True)
        return jsonify({"transcripcion": texto, "ok": bool(texto)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.route("/posta/analizar", methods=["POST"])
def posta_analizar():
    data = request.get_json(silent=True) or {}
    turno_id = data.get("turno_id", "")
    cama = data.get("cama", "")
    nombre = data.get("nombre", "")
    edad = data.get("edad", "")
    dx = data.get("dx", "")
    rol = data.get("rol", "medico")
    try:
        ws = get_posta_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
        filas_paciente = [f for f in datos if len(f) >= 9 and f[1] == turno_id and f[2] == cama]
        if not filas_paciente:
            return jsonify({"error": "Sin transcripciones para este paciente."}), 404
        contexto = "\n".join(f[7] for f in filas_paciente)
        perfil = f"Paciente: {nombre}, {edad} años. Diagnóstico: {dx}. Rol que documenta: {rol}."
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=POSTA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{perfil}\n\nTRANSCRIPCIÓN DEL PASE:\n{contexto}"}]
        )
        evolucion = respuesta.content[0].text
        return jsonify({"evolucion": evolucion, "cama": cama, "nombre": nombre})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/turno", methods=["POST"])
def posta_guardar_turno():
    data = request.get_json(silent=True) or {}
    turno_id = data.get("turno_id", datetime.now().strftime("%Y%m%d%H%M%S"))
    rol = data.get("rol", "medico")
    medico = data.get("medico", "")
    pacientes = data.get("pacientes", [])
    pdf_texto = data.get("pdf_texto", "")
    try:
        ws = get_posta_turnos_sheet()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([turno_id, ts, rol, medico, json.dumps(pacientes, ensure_ascii=False), pdf_texto])
        return jsonify({"ok": True, "turno_id": turno_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/turno/<turno_id>", methods=["GET"])
def posta_get_turno(turno_id):
    try:
        ws = get_posta_turnos_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        for fila in reversed(datos):
            if len(fila) >= 5 and fila[0] == turno_id:
                pacientes = json.loads(fila[4]) if fila[4] else []
                return jsonify({
                    "turno_id": fila[0],
                    "timestamp": fila[1],
                    "rol": fila[2],
                    "medico": fila[3],
                    "pacientes": pacientes,
                    "pdf_texto": fila[5] if len(fila) > 5 else "",
                })
        return jsonify({"error": "Turno no encontrado."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/chat", methods=["POST"])
def posta_chat():
    data = request.get_json(silent=True) or {}
    pregunta = data.get("pregunta", "").strip()
    contexto_paciente = data.get("contexto_paciente", "")
    historial = data.get("historial", [])
    if not pregunta:
        return jsonify({"error": "Falta la pregunta."}), 400
    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        messages = []
        for msg in historial[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({
            "role": "user",
            "content": f"CONTEXTO DEL PACIENTE:\n{contexto_paciente}\n\nPREGUNTA:\n{pregunta}"
        })
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=POSTA_SYSTEM_PROMPT,
            messages=messages,
        )
        return jsonify({"respuesta": respuesta.content[0].text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/consultar", methods=["POST"])
def posta_consultar():
    data = request.get_json(silent=True) or {}
    pregunta = data.get("pregunta", "").strip()
    contexto_paciente = data.get("contexto_paciente", "")
    historial = data.get("historial", [])
    if not pregunta:
        return jsonify({"error": "Falta la pregunta."}), 400
    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        messages = []
        for msg in historial[-6:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({
            "role": "user",
            "content": f"CONTEXTO DEL PACIENTE:\n{contexto_paciente}\n\nPREGUNTA:\n{pregunta}"
        })
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=POSTA_CONSULTOR_PROMPT,
            messages=messages,
        )
        return jsonify({"respuesta": respuesta.content[0].text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/analizar-imagen", methods=["POST"])
def posta_analizar_imagen():
    data = request.get_json(silent=True) or {}
    imagen_base64 = data.get("imagen_base64", "")
    mime_type = data.get("mime_type", "image/jpeg")
    nombre = data.get("nombre", "")
    edad = data.get("edad", "")
    dx = data.get("dx", "")
    if not imagen_base64:
        return jsonify({"error": "Falta la imagen."}), 400
    try:
        anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        respuesta = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=(
                "Sos un médico intensivista experto analizando imágenes clínicas.\n"
                "Analizás la imagen y describís los hallazgos clínicamente relevantes en español,\n"
                "de forma concisa y precisa, como lo haría un médico en una evolución clínica.\n"
                "Identificá el tipo de imagen automáticamente (Rx tórax, ECG, monitor, TAC, laboratorio, etc).\n"
                "Describí solo hallazgos objetivos y relevantes. Sin introducción ni conclusión.\n"
                "Formato: \"[Tipo de imagen]: [hallazgos principales]\""
            ),
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": imagen_base64,
                        }
                    },
                    {
                        "type": "text",
                        "text": f"Contexto del paciente: {nombre}, {edad} años. Diagnóstico: {dx}. Analizá esta imagen clínica e integrala al contexto del paciente."
                    }
                ]
            }]
        )
        return jsonify({"hallazgo": respuesta.content[0].text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/contexto/<turno_id>/<cama>", methods=["GET"])
def posta_get_contexto(turno_id, cama):
    try:
        ws = get_posta_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if todas and todas[0][0].lower() == "timestamp" else todas
        filas = [f for f in datos if len(f) >= 9 and f[1] == turno_id and f[2] == cama]
        if not filas:
            return jsonify({"contexto": "", "fragmentos": 0})
        contexto = "\n".join(f"[{f[0]}] {f[7]}" for f in filas)
        return jsonify({
            "contexto": contexto,
            "fragmentos": len(filas),
            "paciente": {
                "cama": filas[0][2],
                "nombre": filas[0][3],
                "dni": filas[0][4],
                "edad": filas[0][5],
                "dx": filas[0][6],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/paciente/ingresar", methods=["POST"])
def posta_ingresar_paciente():
    data = request.get_json(silent=True) or {}
    servicio_id = data.get("servicio_id", "").strip()
    cama = data.get("cama", "").strip()
    nombre = data.get("nombre", "").strip()
    dni = data.get("dni", "").strip()
    edad = data.get("edad", "").strip()
    dx = data.get("dx", "").strip()
    institucion = data.get("institucion", "").strip()
    if not servicio_id or not cama or not nombre:
        return jsonify({"error": "Faltan datos obligatorios."}), 400
    try:
        ws = get_posta_pacientes_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        for i, fila in enumerate(datos):
            if len(fila) >= 3 and fila[1] == servicio_id and fila[2] == cama and fila[9] == "true":
                id_existente = fila[0]
                return jsonify({"ok": True, "id_paciente": id_existente, "existente": True})
        id_paciente = generar_id_paciente(servicio_id, cama)
        fecha_ingreso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([id_paciente, servicio_id, cama, nombre, dni, edad, dx, institucion, fecha_ingreso, "true", "", ""])
        return jsonify({"ok": True, "id_paciente": id_paciente, "existente": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/servicio/<servicio_id>", methods=["GET"])
def posta_get_servicio(servicio_id):
    try:
        ws_pac = get_posta_pacientes_sheet()
        todas = ws_pac.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        pacientes_activos = []
        for fila in datos:
            if len(fila) >= 10 and fila[1] == servicio_id and fila[9] == "true":
                pacientes_activos.append({
                    "id_paciente": fila[0],
                    "cama": fila[2],
                    "nombre": fila[3],
                    "dni": fila[4],
                    "edad": fila[5],
                    "dx": fila[6],
                    "institucion": fila[7],
                    "fecha_ingreso": fila[8],
                })
        ws_evo = get_posta_evoluciones_sheet()
        todas_evo = ws_evo.get_all_values()
        evo_datos = todas_evo[1:] if len(todas_evo) > 1 else []
        for pac in pacientes_activos:
            evoluciones = [f for f in evo_datos if len(f) >= 1 and f[0] == pac["id_paciente"]]
            pac["total_evoluciones"] = len(evoluciones)
            if evoluciones:
                ultima = evoluciones[-1]
                pac["ultima_evolucion"] = ultima[1] if len(ultima) > 1 else ""
                pac["ultimo_medico"] = ultima[4] if len(ultima) > 4 else ""
        pacientes_activos.sort(key=lambda x: x["cama"])
        return jsonify({"servicio_id": servicio_id, "pacientes": pacientes_activos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/paciente/<id_paciente>", methods=["GET"])
def posta_get_paciente(id_paciente):
    try:
        ws_pac = get_posta_pacientes_sheet()
        todas = ws_pac.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        paciente = None
        for fila in datos:
            if len(fila) >= 1 and fila[0] == id_paciente:
                paciente = {
                    "id_paciente": fila[0],
                    "servicio_id": fila[1],
                    "cama": fila[2],
                    "nombre": fila[3],
                    "dni": fila[4],
                    "edad": fila[5],
                    "dx": fila[6],
                    "institucion": fila[7],
                    "fecha_ingreso": fila[8],
                    "activo": fila[9] == "true",
                }
                break
        if not paciente:
            return jsonify({"error": "Paciente no encontrado."}), 404
        ws_evo = get_posta_evoluciones_sheet()
        todas_evo = ws_evo.get_all_values()
        evo_datos = todas_evo[1:] if len(todas_evo) > 1 else []
        evoluciones = []
        for fila in evo_datos:
            if len(fila) >= 1 and fila[0] == id_paciente:
                evoluciones.append({
                    "fecha": fila[1],
                    "turno": fila[2],
                    "rol": fila[3],
                    "medico": fila[4],
                    "matricula": fila[5],
                    "evolucion": fila[7],
                    "institucion": fila[9] if len(fila) > 9 else "",
                })
        return jsonify({"paciente": paciente, "evoluciones": evoluciones})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/evolucion/guardar", methods=["POST"])
def posta_guardar_evolucion():
    data = request.get_json(silent=True) or {}
    id_paciente = data.get("id_paciente", "").strip()
    turno_id = data.get("turno_id", "").strip()
    turno = data.get("turno", "").strip()
    rol = data.get("rol", "medico").strip()
    medico = data.get("medico", "").strip()
    matricula = data.get("matricula", "").strip()
    servicio_id = data.get("servicio_id", "").strip()
    evolucion = data.get("evolucion", "").strip()
    institucion = data.get("institucion", "").strip()
    if not id_paciente or not evolucion:
        return jsonify({"error": "Faltan datos."}), 400
    try:
        ws = get_posta_evoluciones_sheet()
        fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([id_paciente, fecha, turno, rol, medico, matricula, servicio_id, evolucion, turno_id, institucion])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/paciente/<id_paciente>/egresar", methods=["POST"])
def posta_egresar_paciente(id_paciente):
    try:
        ws = get_posta_pacientes_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        for i, fila in enumerate(datos):
            if len(fila) >= 1 and fila[0] == id_paciente:
                fecha_egreso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ws.update_cell(i + 2, 10, "false")
                ws.update_cell(i + 2, 11, fecha_egreso)
                return jsonify({"ok": True})
        return jsonify({"error": "Paciente no encontrado."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/alerta/crear", methods=["POST"])
def posta_crear_alerta():
    data = request.get_json(silent=True) or {}
    id_paciente = data.get("id_paciente", "").strip()
    servicio_id = data.get("servicio_id", "").strip()
    texto = data.get("texto", "").strip()
    tipo = data.get("tipo", "manual").strip()
    fecha_recordatorio = data.get("fecha_recordatorio", "").strip()
    medico = data.get("medico", "").strip()
    turno_id = data.get("turno_id", "").strip()
    if not id_paciente or not texto:
        return jsonify({"error": "Faltan datos."}), 400
    try:
        ws = get_posta_alertas_sheet()
        import uuid
        alerta_id = str(uuid.uuid4())[:8].upper()
        fecha_creacion = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([alerta_id, id_paciente, servicio_id, texto, tipo, fecha_creacion, fecha_recordatorio, "false", medico, turno_id])
        return jsonify({"ok": True, "id": alerta_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/alertas/<id_paciente>", methods=["GET"])
def posta_get_alertas(id_paciente):
    try:
        ws = get_posta_alertas_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        alertas = []
        for i, fila in enumerate(datos):
            if len(fila) >= 8 and fila[1] == id_paciente and fila[7] == "false":
                alertas.append({
                    "id": fila[0],
                    "texto": fila[3],
                    "tipo": fila[4],
                    "fecha_creacion": fila[5],
                    "fecha_recordatorio": fila[6],
                    "medico": fila[8] if len(fila) > 8 else "",
                    "fila": i + 2
                })
        alertas.sort(key=lambda x: x.get("fecha_recordatorio") or x.get("fecha_creacion"))
        return jsonify({"alertas": alertas})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/alerta/<alerta_id>/cumplir", methods=["POST"])
def posta_cumplir_alerta(alerta_id):
    try:
        ws = get_posta_alertas_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        for i, fila in enumerate(datos):
            if len(fila) >= 1 and fila[0] == alerta_id:
                ws.update_cell(i + 2, 8, "true")
                return jsonify({"ok": True})
        return jsonify({"error": "Alerta no encontrada."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/alertas/servicio/<servicio_id>/hoy", methods=["GET"])
def posta_alertas_hoy(servicio_id):
    from datetime import date
    hoy = date.today().strftime("%Y-%m-%d")
    try:
        ws = get_posta_alertas_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        alertas = []
        for fila in datos:
            if len(fila) >= 8 and fila[2] == servicio_id and fila[7] == "false":
                fecha_rec = fila[6][:10] if fila[6] else ""
                if not fecha_rec or fecha_rec <= hoy:
                    alertas.append({
                        "id": fila[0],
                        "id_paciente": fila[1],
                        "texto": fila[3],
                        "tipo": fila[4],
                        "fecha_recordatorio": fila[6],
                        "medico": fila[8] if len(fila) > 8 else "",
                    })
        return jsonify({"alertas": alertas, "total": len(alertas)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/posta/resumen/<id_paciente>", methods=["GET"])
def posta_get_resumen(id_paciente):
    try:
        ws = get_posta_resumenes_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        from datetime import date
        hoy = date.today().strftime("%Y-%m-%d")
        for fila in reversed(datos):
            if len(fila) >= 3 and fila[0] == id_paciente:
                return jsonify({
                    "resumen": fila[2],
                    "fecha": fila[1],
                    "ultima_actualizacion": fila[3] if len(fila) > 3 else "",
                    "es_hoy": fila[1] == hoy
                })
        return jsonify({"resumen": None, "fecha": None, "es_hoy": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/posta/resumen/<id_paciente>", methods=["POST"])
def posta_guardar_resumen(id_paciente):
    data = request.get_json(silent=True) or {}
    resumen = data.get("resumen", "").strip()
    servicio_id = data.get("servicio_id", "").strip()
    medico = data.get("medico", "").strip()
    if not resumen:
        return jsonify({"error": "Falta el resumen."}), 400
    try:
        ws = get_posta_resumenes_sheet()
        todas = ws.get_all_values()
        datos = todas[1:] if len(todas) > 1 else []
        from datetime import date
        hoy = date.today().strftime("%Y-%m-%d")
        ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for i, fila in enumerate(datos):
            if len(fila) >= 2 and fila[0] == id_paciente and fila[1] == hoy:
                ws.update_cell(i + 2, 3, resumen)
                ws.update_cell(i + 2, 4, ahora)
                return jsonify({"ok": True, "actualizado": True})
        ws.append_row([id_paciente, hoy, resumen, ahora, servicio_id, medico])
        return jsonify({"ok": True, "actualizado": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/javi/audio", methods=["POST"])
def javi_audio():
    if 'audio' not in request.files:
        return jsonify({"error": "No se recibió audio"}), 400
    audio_file = request.files['audio']
    guardia_id = request.form.get('guardia_id', 'sin_id')
    try:
        with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp:
            audio_file.save(tmp.name)
            tmp_path = tmp.name
        with open(tmp_path, 'rb') as f:
            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            import openai
            oai = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            transcripcion = oai.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="es"
            )
        os.unlink(tmp_path)
        return jsonify({"transcripcion": transcripcion.text, "guardia_id": guardia_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/javi/procesar", methods=["POST"])
def javi_procesar():
    data = request.get_json(silent=True) or {}
    transcripcion = data.get("transcripcion", "").strip()
    guardia_id = data.get("guardia_id", "")
    pacientes_actuales = data.get("pacientes_actuales", [])
    if not transcripcion:
        return jsonify({"error": "Sin transcripción"}), 400
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        contexto = f"""GUARDIA ID: {guardia_id}
PACIENTES CONOCIDOS HASTA AHORA:
{chr(10).join([f"- Cama {p.get('cama','?')}: {p.get('nombre','?')} — {p.get('dx','?')}" for p in pacientes_actuales]) if pacientes_actuales else "Ninguno aún"}

FRAGMENTO DE AUDIO TRANSCRIPTO:
{transcripcion}"""
        respuesta = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=JAVI_PROCESADOR_PROMPT,
            messages=[{"role": "user", "content": contexto}]
        )
        texto = respuesta.content[0].text
        texto_limpio = texto.replace('```json', '').replace('```', '').strip()
        import json
        resultado = json.loads(texto_limpio)
        return jsonify(resultado)
    except Exception as e:
        return jsonify({"error": str(e), "mensaje_javi": None, "pacientes_detectados": []}), 500

@app.route("/javi/consulta", methods=["POST"])
def javi_consulta():
    data = request.get_json(silent=True) or {}
    pregunta = data.get("pregunta", "").strip()
    guardia_id = data.get("guardia_id", "")
    pacientes = data.get("pacientes", [])
    if not pregunta:
        return jsonify({"error": "Sin pregunta"}), 400
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        contexto = f"""CONTEXTO DE LA GUARDIA:
Pacientes activos: {len(pacientes)}
{chr(10).join([f"- Cama {p.get('cama','?')}: {p.get('nombre','?')} — {p.get('dx','?')}" for p in pacientes]) if pacientes else ""}

PREGUNTA DEL MÉDICO:
{pregunta}"""
        respuesta = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=JAVI_CONSULTOR_PROMPT,
            messages=[{"role": "user", "content": contexto}]
        )
        return jsonify({"respuesta": respuesta.content[0].text, "guardia_id": guardia_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _ojo_prompt(rubro):
    r = f" (rubro: {rubro})" if rubro else ""
    return f"""Sos un asesor que le habla directo al DUEÑO de un comercio en Argentina, no al encargado. El dueño no se desvela por la operación diaria (reponer stock, pesar productos): se desvela por tres cosas — PLATA que se fuga, ROBO o mermas, y MARGEN. Tu trabajo es mirar la pantalla y decirle algo que él NO sabía y que le toque el bolsillo.

Te paso una foto de la pantalla de un negocio{r}.

Reglas:
- Leé todos los datos visibles (montos, cantidades, productos, tickets, medios de pago).
- En el campo "datos" listá como máximo los 6 ítems más relevantes que viste, no todos los que haya en la pantalla. Priorizá los que sustenten el rojo/amarillo/verde.
- Traducí SIEMPRE lo que ves al idioma del dueño: cuánta plata, por dónde se fuga, qué margen pierde. Un dato de inventario no es "hay poco aceite", es "esto es plata inmovilizada" o "esto es una fuga".
- ROJO: el hallazgo MÁS filoso, el que le pararía el corazón al dueño. Prioridad: señales de robo/merma > plata que se pierde > margen mal aprovechado. Con un número concreto (estimado conservador si hace falta, marcalo). NO uses el rojo para consejos operativos obvios (reponer, pesar) que el encargado ya sabe.
- AMARILLO: un riesgo real que todavía no explotó.
- VERDE: algo que funciona, para que lo sostenga.
- 3 movidas: decisiones de DUEÑO (controlar una fuga, medir plata, cambiar precios, cortar una pérdida), NO tareas de depósito. Cortas.

Devolvé SOLO JSON válido, sin markdown:
{{"rubro":"...","datos":[{{"k":"","v":""}}],"rojo":{{"t":"","d":"","numero":"$ ...","estimado":true}},"amarillo":{{"t":"","d":""}},"verde":{{"t":"","d":""}},"acciones":["","",""]}}
Si no se lee nada útil: {{"error":"No se leen datos claros en la foto."}} Sé conciso pero filoso."""


@app.route("/ojo/analizar", methods=["POST"])
def ojo_analizar():
    data = request.get_json(silent=True) or {}
    b64 = data.get("imagen")
    mime = data.get("mime", "image/png")
    rubro = (data.get("rubro") or "").strip()
    if not b64:
        return jsonify({"error": "No llegó ninguna imagen."}), 400

    anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    for intento in range(2):
        try:
            msg = anthropic_client.messages.create(
                model="claude-sonnet-5",
                max_tokens=5000,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                        {"type": "text", "text": _ojo_prompt(rubro)},
                    ],
                }],
            )
            if msg.stop_reason == "max_tokens":
                print(f"OJO intento {intento + 1}: corte por stop_reason=max_tokens, reintentando" if intento == 0 else f"OJO intento {intento + 1}: corte por stop_reason=max_tokens de nuevo")
                continue
            text = "".join(b.text for b in msg.content if b.type == "text")
            clean = text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            return jsonify(parsed)
        except json.JSONDecodeError as e:
            print(f"OJO intento {intento + 1}: JSON invalido -", e)
            continue
        except Exception as e:
            print("OJO error:", e)
            return jsonify({"error": "Falló el análisis. Intentá de nuevo."}), 500

    return jsonify({"error": "Estamos procesando tu pantalla, probá de nuevo en unos segundos."})
