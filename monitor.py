#!/usr/bin/env python3
"""Monitor del estado de un tramite consular (SuTRAMITE / MAEC).

Human-in-the-loop: cada consulta real al servidor la valida el usuario
resolviendo el captcha que le llega por Telegram. El script se encarga del
resto: acceso por resguardo, parseo de la pagina de estado, deteccion de
cambios y aviso.

Uso previsto: lanzado por un cron (GitHub Actions o el Programador de
tareas de Windows) cada 30 min. El propio script decide si "toca" comprobar
(intervalo minimo + horario activo), asi que la mayoria de ejecuciones
terminan sin molestar a nadie.

Configuracion: variables de entorno (BOT_TOKEN, CHAT_ID, TRAMITE_ID,
ANIO_NAC, ...) o, si no estan, config.ini. NOTIFY_BOT_TOKEN + NOTIFY_CHAT_ID
(opcionales) mandan los avisos de resultado a otro bot; el captcha sigue en
BOT_TOKEN/CHAT_ID. El estado entre ejecuciones se guarda en state.json local
o, si STATE_GIST_ID + GIST_TOKEN estan puestos, en un gist privado.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import certifi
import requests
from bs4 import BeautifulSoup

BASE = "https://sutramiteconsular.maec.es/"
CAPTCHA_URL = BASE + "CaptchaHome.aspx"

# Si fallas el captcha, se pide otro en la misma ejecucion (no hay que
# esperar al siguiente cron). El primer intento usa el timeout normal;
# los reintentos, uno mas corto para no agotar el limite del job.
CAPTCHA_MAX_ATTEMPTS = 3
CAPTCHA_RETRY_TIMEOUT = 180

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.ini"
STATE_PATH = HERE / "state.json"
SNAP_DIR = HERE / "snapshots"
INTERMEDIATE_PEM = HERE / "maec-intermediate.pem"
CA_BUNDLE = HERE / "_maec_cabundle.pem"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Etiqueta de fecha larga en castellano que la web pinta en cada pagina.
# Hay que quitarla antes de comparar o cada dia parece que "cambio algo".
SPANISH_DATE = re.compile(
    r"(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)"
    r"[^\n.]*?\d{4}",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config / estado
# --------------------------------------------------------------------------- #

class Settings:
    """Lee de variables de entorno primero, luego de config.ini."""

    def __init__(self) -> None:
        cfg = configparser.ConfigParser()
        if CONFIG_PATH.exists():
            cfg.read(CONFIG_PATH, encoding="utf-8")

        def val(env: str, section: str, key: str, default: str | None = None):
            v = os.environ.get(env)
            if v is not None and v.strip() != "":
                return v.strip()
            if cfg.has_option(section, key):
                return cfg.get(section, key).strip()
            return default

        self.bot_token = val("BOT_TOKEN", "telegram", "bot_token")
        self.chat_id = val("CHAT_ID", "telegram", "chat_id")
        self.captcha_reply_timeout = int(val(
            "CAPTCHA_REPLY_TIMEOUT_SECONDS", "telegram",
            "captcha_reply_timeout_seconds", "900"))
        # Bot aparte para los avisos del resultado (linea base / cambio /
        # sin cambios). Si no se define, va al mismo bot del captcha.
        self.notify_bot_token = val("NOTIFY_BOT_TOKEN", "notify", "bot_token")
        self.notify_chat_id = val("NOTIFY_CHAT_ID", "notify", "chat_id")
        self.tipo = (val("TRAMITE_TIPO", "tramite", "tipo", "VISADO")).upper()
        self.identificador = val("TRAMITE_ID", "tramite", "identificador")
        self.anio_nacimiento = val("ANIO_NAC", "tramite", "anio_nacimiento")
        self.min_interval_minutes = int(val(
            "MIN_INTERVAL_MINUTES", "schedule", "min_interval_minutes", "180"))
        self.active_hour_start = int(val(
            "ACTIVE_HOUR_START", "schedule", "active_hour_start", "8"))
        self.active_hour_end = int(val(
            "ACTIVE_HOUR_END", "schedule", "active_hour_end", "20"))
        # Backend de estado: gist privado si estan las dos variables.
        self.state_gist_id = os.environ.get("STATE_GIST_ID", "").strip()
        self.gist_token = os.environ.get("GIST_TOKEN", "").strip()
        # OCR (Worker propio). Si no se define, el captcha se manda al chat y el usuario lo resuelve.
        self.ocr_url = val("OCR_URL", "ocr", "url")
        self.ocr_key = val("OCR_KEY", "ocr", "key")

        missing = [n for n, v in (
            ("BOT_TOKEN/bot_token", self.bot_token),
            ("CHAT_ID/chat_id", self.chat_id),
            ("TRAMITE_ID/identificador", self.identificador),
            ("ANIO_NAC/anio_nacimiento", self.anio_nacimiento),
        ) if not v]
        if missing:
            sys.exit("Faltan ajustes (ni env ni config.ini): "
                     + ", ".join(missing))


GIST_STATE_FILE = "state.json"


def _gist_headers(s: Settings) -> dict:
    return {"Authorization": f"Bearer {s.gist_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def load_state(s: Settings) -> dict:
    if s.state_gist_id:
        r = requests.get(f"https://api.github.com/gists/{s.state_gist_id}",
                         headers=_gist_headers(s), timeout=30)
        r.raise_for_status()
        f = (r.json().get("files") or {}).get(GIST_STATE_FILE)
        if not f:
            return {}
        content = f.get("content", "")
        if f.get("truncated") and f.get("raw_url"):
            content = requests.get(f["raw_url"], headers=_gist_headers(s),
                                   timeout=30).text
        try:
            return json.loads(content) if content.strip() else {}
        except json.JSONDecodeError:
            return {}
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(s: Settings, state: dict) -> None:
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    if s.state_gist_id:
        r = requests.patch(
            f"https://api.github.com/gists/{s.state_gist_id}",
            headers=_gist_headers(s),
            json={"files": {GIST_STATE_FILE: {"content": payload}}},
            timeout=30)
        r.raise_for_status()
        return
    STATE_PATH.write_text(payload, encoding="utf-8")


def ca_bundle() -> str:
    """certifi + intermediate FNMT (el servidor no la envia en el handshake)."""
    if not INTERMEDIATE_PEM.exists():
        return certifi.where()
    if (not CA_BUNDLE.exists()
            or CA_BUNDLE.stat().st_mtime < INTERMEDIATE_PEM.stat().st_mtime):
        data = Path(certifi.where()).read_text(encoding="utf-8")
        data += "\n" + INTERMEDIATE_PEM.read_text(encoding="utf-8")
        CA_BUNDLE.write_text(data, encoding="utf-8")
    return str(CA_BUNDLE)


# --------------------------------------------------------------------------- #
# OCR opcional (Worker propio). Devuelve la lectura del captcha como texto;
# monitor.py la publica en el MISMO chat/bot del captcha, como pista. El numero
# valido lo sigue tecleando el usuario (esto no lo usa como captcha).
# --------------------------------------------------------------------------- #

OCR_TIMEOUT = 25


def ocr_leer(url: str | None, key: str | None, img: bytes) -> str | None:
    """Pide al Worker OCR su lectura del captcha. Devuelve el texto o None.

    Se traga cualquier fallo (log) y nunca bloquea la ronda.
    """
    if not url or not key:
        return None
    try:
        r = requests.post(
            url.rstrip("/") + "/ocr",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": ("captcha.jpg", img, "application/octet-stream")},
            timeout=OCR_TIMEOUT,
        )
        if not r.ok:
            log(f"Worker OCR respondio {r.status_code}: {r.text[:150]}")
            return None
        numeros = (r.json().get("numeros") or "").strip()
        log(f"OCR leyo: {numeros!r}")
        return numeros or None
    except (requests.RequestException, ValueError) as e:
        log(f"OCR no disponible: {e}")
        return None


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)

    def _call(self, method: str, **kw):
        r = requests.post(f"{self.api}/{method}", timeout=40, **kw)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data}")
        return data["result"]

    def send_message(self, text: str) -> None:
        self._call("sendMessage", data={
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        })

    def send_document(self, data: bytes, filename: str,
                      caption: str = "") -> None:
        # sendDocument (no sendPhoto): Telegram no recomprime el fichero, asi
        # que el captcha llega con la calidad original.
        payload = {"chat_id": self.chat_id,
                   "disable_content_type_detection": "true"}
        if caption:
            payload["caption"] = caption
        self._call("sendDocument", data=payload,
                   files={"document": (filename, data,
                                       "application/octet-stream")})

    def _get_updates(self, offset: int, timeout: int):
        r = requests.get(f"{self.api}/getUpdates",
                         params={"offset": offset, "timeout": timeout},
                         timeout=timeout + 15)
        r.raise_for_status()
        return r.json().get("result", [])

    def wait_for_reply(self, deadline_s: int) -> tuple[str | None, bool]:
        """Espera un mensaje del chat: 4-6 digitos, o 'skip'/'no' para abortar.

        Devuelve (codigo|None, abort_bool). Ignora todo lo anterior a la llamada.
        """
        seen = self._get_updates(offset=-1, timeout=0)
        offset = (seen[-1]["update_id"] + 1) if seen else 0
        end = time.time() + deadline_s
        digits = re.compile(r"^\s*(\d{4,6})\s*$")
        abort = re.compile(r"^\s*(skip|no|nada|salta|cancelar?)\s*$", re.I)
        while time.time() < end:
            try:
                updates = self._get_updates(offset=offset, timeout=25)
            except requests.RequestException as e:
                log(f"getUpdates fallo transitorio: {e}")
                time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                if str(msg.get("chat", {}).get("id")) != self.chat_id:
                    continue
                text = msg.get("text", "")
                if abort.match(text):
                    return None, True
                m = digits.match(text)
                if m:
                    return m.group(1), False
        return None, False


# --------------------------------------------------------------------------- #
# SuTRAMITE
# --------------------------------------------------------------------------- #

class SuTramite:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept-Language": "es-ES,es;q=0.9"})
        self.verify = ca_bundle()

    def load_form(self) -> dict:
        r = self.s.get(BASE, timeout=40, verify=self.verify)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form") or soup
        fields = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name and inp.get("type", "text").lower() in (
                    "hidden", "text", "password"):
                fields[name] = inp.get("value", "") or ""
        if "__VIEWSTATE" not in fields:
            raise RuntimeError("No encuentro __VIEWSTATE; la pagina cambio de "
                               "estructura.")
        return fields

    def fetch_captcha(self) -> bytes:
        r = self.s.get(CAPTCHA_URL, params={"r": random.randint(1, 9999)},
                       timeout=40, verify=self.verify,
                       headers={"Referer": BASE})
        r.raise_for_status()
        if "image" not in r.headers.get("Content-Type", ""):
            raise RuntimeError("CaptchaHome.aspx no devolvio una imagen.")
        return r.content

    def submit(self, fields: dict, *, tipo: str, identificador: str,
               anio_nacimiento: str, captcha: str) -> str:
        data = dict(fields)
        data.update({
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "infServicio": tipo,
            "txIdentificador": identificador,
            "txIdAcceda": "",
            "txtFechaNacimiento": str(anio_nacimiento),
            "imgcaptcha": captcha,
            "MensajeError": "",
            "PasswCifra": "",
            "hPassword": "", "hUsuario": "", "hPanelProgreso": "",
            "hServicio": "",
            # boton tipo imagen -> el navegador manda las coordenadas del click
            "imgVerSuTramite.x": str(random.randint(5, 90)),
            "imgVerSuTramite.y": str(random.randint(3, 18)),
        })
        r = self.s.post(BASE, data=data, timeout=60, verify=self.verify,
                        headers={"Referer": BASE,
                                 "Content-Type":
                                     "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.text


# --------------------------------------------------------------------------- #
# Parseo de la respuesta
# --------------------------------------------------------------------------- #

# La pagina de estado es ASP.NET WebForms: muchos <div> "capa" ocultos y solo
# uno visible. El hidden 'capaParaMostrar' dice cual. En vez de comparar todo
# el HTML (lleno de plantillas ocultas y VIEWSTATE), extraemos unos pocos
# campos con sentido y comparamos eso.

STATUS_PAGE_MARKER = "ContentPlaceHolderConsulta_capaParaMostrar"
FORM_MARKERS = ("SELECCIONE EL TIPO DE SOLICITUD", 'id="imagenCaptcha"',
                'name="imgcaptcha"')

# id del <span>/<input> en la pagina  ->  clave legible en nuestro resumen
STATUS_SPANS = {
    "ContentPlaceHolderConsulta_nombre": "ciudadano",
    "ContentPlaceHolderConsulta_fechasol": "fecha_solicitud",
    "ContentPlaceHolderConsulta_tipoTramite": "tramite",
    "ContentPlaceHolderConsulta_TituloEstado": "estado",
    "ContentPlaceHolderConsulta_DescEstado": "estado_detalle",
    "ContentPlaceHolderConsulta_diahora": "cita_dia_hora",
    "ContentPlaceHolderConsulta_estadoCita": "cita_estado",
    "ContentPlaceHolderConsulta_numturno": "cita_turno",
    "ContentPlaceHolderConsulta_motivoCita": "cita_motivo",
    "ContentPlaceHolderConsulta_fechaCitaNoPresentada": "cita_np_fecha",
    "ContentPlaceHolderConsulta_estadoCitaNoPresentada": "cita_np_estado",
    "ContentPlaceHolderConsulta_fechaLimiteSubsanar": "limite_subsanar",
    "ContentPlaceHolderConsulta_DocPendiente": "doc_pendiente",
    "ContentPlaceHolderConsulta_fechaCitaPendienteRes": "situacion_fecha",
    "ContentPlaceHolderConsulta_estadoCitaPendienteRes": "situacion_estado",
    "ContentPlaceHolderConsulta_estadoCitaResuelta": "resolucion",
    "ContentPlaceHolderConsulta_fechaCitaResuelta": "resolucion_fecha",
    "ContentPlaceHolderConsulta_fechaLimiteFirmar": "limite_firmar",
    "ContentPlaceHolderConsulta_estadoCitaCaducada": "caducado_estado",
    "ContentPlaceHolderConsulta_fechaCitaCaducada": "caducado_fecha",
}


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def is_status_page(html: str) -> bool:
    return STATUS_PAGE_MARKER in html


def looks_like_login_page(html: str) -> bool:
    return any(m in html for m in FORM_MARKERS)


def extract_alert(html: str) -> str | None:
    m = re.search(r"alert\(['\"](.+?)['\"]\)", html, re.S)
    if m:
        return _clean(m.group(1).replace("\\n", " ")) or None
    soup = BeautifulSoup(html, "html.parser")
    for eid in ("MensajeError", "ContentPlaceHolderConsulta_mostrarError"):
        err = soup.find(id=eid)
        if err and _clean(err.get("value", "")):
            return _clean(err["value"])
    return None


def _grid_rows(soup: BeautifulSoup, grid_id: str) -> list[str]:
    table = soup.find(id=grid_id)
    if not table:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:  # salta la cabecera
        cells = [_clean(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(" | ".join(cells))
    return rows


def parse_status(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}
    cap = soup.find(id="ContentPlaceHolderConsulta_capaParaMostrar")
    data["panel"] = _clean(cap.get("value", "")) if cap else ""
    tip = soup.find(id="ContentPlaceHolderConsulta_TipoServicio")
    if tip and _clean(tip.get("value", "")):
        data["tipo_servicio"] = _clean(tip["value"])
    for el_id, key in STATUS_SPANS.items():
        el = soup.find(id=el_id)
        if el is None:
            continue
        val = _clean(el.get_text(" ", strip=True))
        if val:
            data[key] = val
    data["notificaciones"] = _grid_rows(
        soup, "ContentPlaceHolderConsulta_GridNotificaciones")
    data["citas_anteriores"] = _grid_rows(
        soup, "ContentPlaceHolderConsulta_GridViewCitasAnteriores")
    return data


def status_digest(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_EMPTY_GRID = ("NO TIENE NOTIFICACIONES", "NO TIENE CITAS ANTERIORES")


def format_status(data: dict) -> str:
    lines = [f"Panel: {data.get('panel') or '?'}"]
    if data.get("estado"):
        det = data.get("estado_detalle", "")
        lines.append(f"Estado: {data['estado']}"
                     + (f" — {det}" if det else ""))
    if data.get("tramite"):
        lines.append(f"Tramite: {data['tramite']} "
                     f"(solicitud {data.get('fecha_solicitud', '?')})")
    for label, key in (("Situacion", "situacion_estado"),
                       ("Resolucion", "resolucion"),
                       ("Cita dia/hora", "cita_dia_hora"),
                       ("Cita estado", "cita_estado"),
                       ("Cita motivo", "cita_motivo"),
                       ("Limite subsanar", "limite_subsanar"),
                       ("Doc pendiente", "doc_pendiente"),
                       ("Limite firmar", "limite_firmar"),
                       ("Caducado", "caducado_estado")):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    notis = [n for n in data.get("notificaciones", []) if n not in _EMPTY_GRID]
    lines.append("Notificaciones: "
                 + ("; ".join(notis) if notis else "(ninguna)"))
    return "\n".join(lines)


def diff_status(old: dict, new: dict) -> str:
    out = []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        if o != n:
            out.append(f"• {k}:\n   antes: {o!r}\n   ahora: {n!r}")
    return "\n".join(out) or "(sin diferencias en los campos vigilados)"


def _ts(epoch: float | None) -> str:
    """Hora local del runner (America/Havana via TZ en el workflow)."""
    if not epoch:
        return "desconocido"
    return datetime.fromtimestamp(epoch).strftime("%d/%m %H:%M")


HISTORY_MAX = 120


def add_history(state: dict, ts: float, data: dict, cambio: bool) -> None:
    """Anota una revision en state["history"] (lo lee el Worker para /historial)."""
    hist = state.get("history", [])
    hist.append({
        "ts": ts,
        "estado": data.get("estado", ""),
        "detalle": data.get("estado_detalle", ""),
        "cambio": cambio,
    })
    state["history"] = hist[-HISTORY_MAX:]


def snapshot(html: str, tag: str) -> Path:
    SNAP_DIR.mkdir(exist_ok=True)
    p = SNAP_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.html"
    p.write_text(html, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Logica principal
# --------------------------------------------------------------------------- #

def due_for_check(s: Settings, state: dict) -> tuple[bool, str]:
    now = datetime.now()
    start, end = s.active_hour_start, s.active_hour_end
    if not (start <= now.hour < end):
        return False, f"fuera de horario activo ({start}:00-{end}:00)"
    last = state.get("last_check_ts")
    if last:
        mins = (time.time() - last) / 60
        if mins < s.min_interval_minutes:
            return False, (f"ultima comprobacion hace {mins:.0f} min "
                           f"(< {s.min_interval_minutes})")
    return True, "toca"


def run_once(force: bool = False) -> int:
    s = Settings()
    state = load_state(s)

    tg = Telegram(s.bot_token, s.chat_id)          # captcha + errores
    if s.notify_bot_token and s.notify_chat_id:
        notifier = Telegram(s.notify_bot_token, s.notify_chat_id)
    else:
        notifier = tg                              # mismo bot si no hay otro
    tipo, identificador, anio = s.tipo, s.identificador, s.anio_nacimiento
    reply_timeout = s.captcha_reply_timeout

    # Los comandos (/estado /historial /revisar) los atiende el Worker de
    # Cloudflare via webhook, no este script. /revisar llega aqui como
    # force=True (input del workflow_dispatch).

    ok, why = due_for_check(s, state)
    if force:
        ok, why = True, "forzado"
    if not ok:
        log(f"No toca comprobar: {why}")
        save_state(s, state)
        return 0
    log(f"Comprobacion: {why}")

    site = SuTramite()
    html = None
    motivo = "captcha incorrecto"
    for intento in range(1, CAPTCHA_MAX_ATTEMPTS + 1):
        try:
            fields = site.load_form()
            img = site.fetch_captcha()
        except Exception as e:
            log(f"Error preparando la consulta: {e}")
            tg.send_message(f"⚠️ Monitor visado: no pude cargar la web ({e}). "
                            f"Reintento en la proxima ventana.")
            save_state(s, state)
            return 1

        if intento > 1:
            tg.send_message(f"❌ Captcha incorrecto. Intento "
                            f"{intento}/{CAPTCHA_MAX_ATTEMPTS}:")
        tg.send_document(img, "captcha.jpg")
        # Pista del OCR en el mismo chat/bot; el usuario la verifica y teclea.
        sugerencia = ocr_leer(s.ocr_url, s.ocr_key, img)
        if sugerencia:
            tg.send_message(f"🔎 OCR (verifica): {sugerencia}")

        espera = reply_timeout if intento == 1 else CAPTCHA_RETRY_TIMEOUT
        code, abort = tg.wait_for_reply(espera)

        if abort:
            log("Usuario aborto la ronda.")
            state["last_check_ts"] = time.time()
            save_state(s, state)
            return 0
        if not code:
            log("Sin respuesta al captcha.")
            tg.send_message("⏳ No recibi el captcha a tiempo. Lo reintento "
                            "en la proxima ventana.")
            save_state(s, state)
            return 0

        try:
            resp = site.submit(fields, tipo=tipo, identificador=identificador,
                               anio_nacimiento=anio, captcha=code)
        except Exception as e:
            log(f"Error en el POST: {e}")
            tg.send_message(f"⚠️ Monitor visado: fallo al enviar ({e}).")
            return 1

        if is_status_page(resp):
            html = resp
            break
        snap = snapshot(resp, "rechazado")
        motivo = extract_alert(resp) or "captcha incorrecto o datos no reconocidos"
        log(f"Intento {intento}: no es pagina de estado ({motivo}). "
            f"Snapshot: {snap.name}")

    state["last_check_ts"] = time.time()
    state.pop("force", None)

    if html is None:
        tg.send_message(f"❌ {CAPTCHA_MAX_ATTEMPTS} intentos de captcha "
                        f"fallidos ({motivo}). Reintento en la proxima ventana.")
        save_state(s, state)
        return 0

    snap = snapshot(html, "estado")
    current = parse_status(html)
    digest = status_digest(current)
    log(f"Pagina de estado OK. digest={digest[:12]} snapshot={snap.name}")

    prev_digest = state.get("status_digest")
    prev_data = state.get("status_data", {})
    prev_ok_ts = state.get("last_ok_ts")
    now_ts = time.time()
    cambio = prev_digest is not None and prev_digest != digest
    state["status_digest"] = digest
    state["status_data"] = current
    state["last_ok_ts"] = now_ts
    add_history(state, now_ts, current, cambio)
    save_state(s, state)

    if prev_digest is None:
        notifier.send_message("✅ Linea base capturada. Solo aviso cuando "
                              "cambie (o si lo pides con /revisar).\n\n"
                              + format_status(current))
    elif cambio:
        notifier.send_message(
            "\U0001f514 CAMBIO en el tramite\n"
            f"detectado: {_ts(now_ts)}\n"
            f"revision anterior sin cambios: {_ts(prev_ok_ts)}\n\n"
            + format_status(current)
            + "\n\n--- que cambio ---\n"
            + diff_status(prev_data, current))
    elif force:
        # revision explicita (/revisar, --now, dispatch con force): confirma
        notifier.send_message(f"✓ Revisado, sin cambios ({_ts(now_ts)}).\n\n"
                              + format_status(current))
    else:
        log("Sin cambios (no se notifica).")

    return 0


def run_console() -> int:
    """Sin Telegram: guarda el captcha, lo abres, lo tecleas aqui.

    Util para la primera prueba y para depurar el POST / el parser.
    """
    s = Settings()

    site = SuTramite()
    fields = site.load_form()
    img = site.fetch_captcha()
    cap_path = HERE / "captcha.jpg"
    cap_path.write_bytes(img)
    print(f"Captcha guardado en: {cap_path}")
    try:
        os.startfile(cap_path)  # type: ignore[attr-defined]
    except Exception:
        pass
    code = input("Numeros del captcha: ").strip()

    html = site.submit(fields, tipo=s.tipo, identificador=s.identificador,
                       anio_nacimiento=s.anio_nacimiento, captcha=code)
    snap = snapshot(html, "console")
    print(f"\nSnapshot: {snap}")
    if not is_status_page(html):
        print(f"\n=> No es la pagina de estado. Motivo: "
              f"{extract_alert(html) or 'captcha/datos incorrectos'}")
        return 0
    data = parse_status(html)
    print("\n=> Pagina de estado. Resumen:\n")
    print(format_status(data))
    print(f"\ndigest={status_digest(data)[:12]}")
    print("\nJSON completo de campos vigilados:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    force = "--now" in args or "--test" in args
    try:
        if "--console" in args:
            sys.exit(run_console())
        sys.exit(run_once(force=force))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR no controlado: {exc}")
        sys.exit(1)
