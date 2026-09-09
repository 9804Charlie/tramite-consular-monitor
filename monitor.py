#!/usr/bin/env python3
"""Monitor del estado de un tramite consular (SuTRAMITE / MAEC).

Human-in-the-loop: cada consulta real al servidor la valida el usuario
resolviendo el captcha que le llega por Telegram. El script se encarga del
resto: acceso por resguardo, parseo de la pagina de estado, deteccion de
cambios y aviso.

Uso previsto: lanzado por el Programador de tareas de Windows cada 30 min.
El propio script decide si "toca" comprobar (intervalo minimo + horario
activo), asi que la mayoria de ejecuciones terminan sin molestar a nadie.
"""
from __future__ import annotations

import configparser
import hashlib
import json
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

def load_config() -> configparser.ConfigParser:
    if not CONFIG_PATH.exists():
        sys.exit(f"Falta {CONFIG_PATH}. Copia config.example.ini a config.ini "
                 f"y rellena los datos.")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


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

    def send_photo(self, img: bytes, caption: str) -> None:
        self._call("sendPhoto",
                   data={"chat_id": self.chat_id, "caption": caption},
                   files={"photo": ("captcha.jpg", img, "image/jpeg")})

    def _get_updates(self, offset: int, timeout: int):
        r = requests.get(f"{self.api}/getUpdates",
                         params={"offset": offset, "timeout": timeout},
                         timeout=timeout + 15)
        r.raise_for_status()
        return r.json().get("result", [])

    def wait_for_reply(self, deadline_s: int):
        """Espera un mensaje del chat: 4-6 digitos, o 'skip'/'no' para abortar.

        Devuelve (codigo|None, abort_bool).
        """
        # Marca de agua: ignora todo lo anterior a esta llamada.
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


def snapshot(html: str, tag: str) -> Path:
    SNAP_DIR.mkdir(exist_ok=True)
    p = SNAP_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}.html"
    p.write_text(html, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Logica principal
# --------------------------------------------------------------------------- #

def due_for_check(cfg, state) -> tuple[bool, str]:
    now = datetime.now()
    start = cfg.getint("schedule", "active_hour_start", fallback=8)
    end = cfg.getint("schedule", "active_hour_end", fallback=20)
    if not (start <= now.hour < end):
        return False, f"fuera de horario activo ({start}:00-{end}:00)"
    min_gap = cfg.getint("schedule", "min_interval_minutes", fallback=180)
    last = state.get("last_check_ts")
    if last:
        mins = (time.time() - last) / 60
        if mins < min_gap:
            return False, f"ultima comprobacion hace {mins:.0f} min (< {min_gap})"
    return True, "toca"


def run_once(force: bool = False) -> int:
    cfg = load_config()
    state = load_state()

    tg = Telegram(cfg["telegram"]["bot_token"], cfg["telegram"]["chat_id"])
    tipo = cfg["tramite"].get("tipo", "VISADO").upper()
    identificador = cfg["tramite"]["identificador"].strip()
    anio = cfg["tramite"]["anio_nacimiento"].strip()
    reply_timeout = cfg.getint("telegram", "captcha_reply_timeout_seconds",
                               fallback=900)

    ok, why = due_for_check(cfg, state)
    if force:
        ok, why = True, "forzado (--now)"
    if not ok:
        log(f"No toca comprobar: {why}")
        return 0
    log(f"Comprobacion: {why}")

    site = SuTramite()
    try:
        fields = site.load_form()
        img = site.fetch_captcha()
    except Exception as e:
        log(f"Error preparando la consulta: {e}")
        tg.send_message(f"⚠️ Monitor visado: no pude cargar la web "
                        f"({e}). Reintento en la proxima ventana.")
        return 1

    tg.send_photo(img, "Monitor visado — escribe los numeros del captcha "
                       "(o 'skip' para saltar esta ronda).")
    code, abort = tg.wait_for_reply(reply_timeout)
    if abort:
        log("Usuario aborto la ronda.")
        state["last_check_ts"] = time.time()
        save_state(state)
        return 0
    if not code:
        log("Sin respuesta al captcha.")
        tg.send_message("⏳ No recibi el captcha a tiempo. Lo reintento en "
                        "la proxima ventana.")
        return 0

    try:
        html = site.submit(fields, tipo=tipo, identificador=identificador,
                           anio_nacimiento=anio, captcha=code)
    except Exception as e:
        log(f"Error en el POST: {e}")
        tg.send_message(f"⚠️ Monitor visado: fallo al enviar ({e}).")
        return 1

    state["last_check_ts"] = time.time()
    state.pop("force", None)

    if not is_status_page(html):
        snap = snapshot(html, "rechazado")
        msg = extract_alert(html) or "captcha incorrecto o datos no reconocidos"
        log(f"Respuesta != pagina de estado ({msg}). Snapshot: {snap.name}")
        tg.send_message(f"❌ No entro: {msg}. Reintento en la proxima ventana.")
        save_state(state)
        return 0

    snap = snapshot(html, "estado")
    current = parse_status(html)
    digest = status_digest(current)
    log(f"Pagina de estado OK. digest={digest[:12]} snapshot={snap.name}")

    prev_digest = state.get("status_digest")
    prev_data = state.get("status_data", {})
    state["status_digest"] = digest
    state["status_data"] = current
    state["last_ok_ts"] = time.time()
    save_state(state)

    if prev_digest is None:
        tg.send_message("✅ Linea base capturada. A partir de ahora solo te "
                        "aviso cuando cambie.\n\n" + format_status(current))
    elif prev_digest != digest:
        tg.send_message("\U0001f514 CAMBIO en el tramite\n\n"
                        + format_status(current)
                        + "\n\n--- que cambio ---\n"
                        + diff_status(prev_data, current))
    else:
        log("Sin cambios.")
        tg.send_message("✓ Revisado, sin cambios.")

    return 0


def run_console() -> int:
    """Sin Telegram: guarda el captcha, lo abres, lo tecleas aqui.

    Util para la primera prueba y para depurar el POST / el parser.
    """
    cfg = load_config()
    tipo = cfg["tramite"].get("tipo", "VISADO").upper()
    identificador = cfg["tramite"]["identificador"].strip()
    anio = cfg["tramite"]["anio_nacimiento"].strip()

    site = SuTramite()
    fields = site.load_form()
    img = site.fetch_captcha()
    cap_path = HERE / "captcha.jpg"
    cap_path.write_bytes(img)
    print(f"Captcha guardado en: {cap_path}")
    try:
        import os
        os.startfile(cap_path)  # type: ignore[attr-defined]
    except Exception:
        pass
    code = input("Numeros del captcha: ").strip()

    html = site.submit(fields, tipo=tipo, identificador=identificador,
                       anio_nacimiento=anio, captcha=code)
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
