# visa-monitor

Vigila el estado de un trámite consular en `sutramiteconsular.maec.es`
(vía "Acceso por Resguardo") y avisa por Telegram **solo cuando algo cambia**.

## Arquitectura

Tres piezas, todo gratis:

| Pieza | Rol | Dónde |
|---|---|---|
| **Worker** (`cloudflare/worker.js`) | Siempre vivo. Webhook de Telegram (comandos), cron cada 15 min, y lee la memoria | Cloudflare Workers |
| **`monitor.py`** | El "obrero": resuelve el captcha contigo, consulta el MAEC y **actualiza** el estado + historial | GitHub Actions |
| **gist privado** | Memoria permanente: `state.json` (estado actual + historial de 120 revisiones) | GitHub Gist |

El Worker no hace scraping; `monitor.py` no escucha comandos. Cada uno hace
lo que su plataforma hace bien.

## Cómo funciona (human-in-the-loop)

La vía del resguardo lleva un captcha numérico. **No se resuelve
automáticamente** — cada consulta real al servidor la validas tú:

1. El **cron** del Worker (cada 15 min) dispara el workflow `monitor`.
2. `monitor.py` mira si "toca" (`MIN_INTERVAL_MINUTES`, por defecto 12). Si no
   toca, termina sin hacer nada.
3. Si toca: carga la web, descarga el captcha y te lo manda a **@Revisor98bot**.
4. Respondes con los 4–6 dígitos (`skip` para saltar). Si fallas, se
   reintenta en la siguiente pasada del cron (~15 min) o con `/revisar`.
5. `monitor.py` envía el formulario, parsea la página de estado, la compara
   con la última guardada en el gist y:
   - primera vez → guarda la "línea base" y te la enseña;
   - si **cambió** → 🔔 aviso con hora de detección, hora de la revisión
     anterior sin cambios, y qué campo cambió;
   - si no cambió → **silencio** (salvo que la revisión fuera explícita).

Ninguna petición al servidor va sin que tú hayas resuelto el captcha.

## Comandos — al bot de avisos (@Rocy_tramite_bot)

Los atiende el **Worker** por webhook → respuesta **al instante**, no dependen
del cron.

| Comando | Efecto |
|---|---|
| `/estado` | Última lectura guardada y cuándo se tomó (hora de La Habana). Lee el gist, no toca la web. |
| `/historial` | Últimas ~20 revisiones con su hora y estado, marcando los cambios. |
| `/revisar` | Dispara `monitor.py` **ya**, sin esperar al cron: captcha a @Revisor98bot + resultado. Avisa aunque no haya cambios. |
| `/start` | Confirma que el bot está vivo. |

Aviso de resultado:
- revisión **automática** (cron) → solo si hay cambio;
- revisión **explícita** (`/revisar`, `--now`) → siempre, con "sin cambios".

## Puesta en marcha

- **Nube (lo normal):** [docs/DEPLOY_ACTIONS.md](docs/DEPLOY_ACTIONS.md) —
  secrets del repo, gist de estado, Worker + webhook, cron.
- **Local (para probar / depurar):**

  ```powershell
  cd "bot revisor"
  py -3 -m pip install -r requirements.txt
  copy config.example.ini config.ini      # rellena token, chat_id, datos del trámite
  py -3 monitor.py --now                   # fuerza una consulta ya
  py -3 monitor.py --console               # sin Telegram: captcha por consola
  ```

## Configuración

`monitor.py` lee **variables de entorno** primero, y si no están, `config.ini`:

| Variable | Para qué |
|---|---|
| `BOT_TOKEN` / `CHAT_ID` | bot del captcha (@Revisor98bot) |
| `NOTIFY_BOT_TOKEN` / `NOTIFY_CHAT_ID` | bot de avisos (@Rocy_tramite_bot); si faltan, todo va al del captcha |
| `TRAMITE_ID` / `ANIO_NAC` / `TRAMITE_TIPO` | datos del resguardo |
| `MIN_INTERVAL_MINUTES` | intervalo mínimo entre consultas reales (12) |
| `ACTIVE_HOUR_START` / `ACTIVE_HOUR_END` | ventana horaria (0–24 = siempre) |
| `CAPTCHA_REPLY_TIMEOUT_SECONDS` | espera del primer captcha (600) |
| `STATE_GIST_ID` / `GIST_TOKEN` | memoria en gist (si faltan, `state.json` local) |
| `OCR_URL` / `OCR_KEY` | *(opcional)* Worker OCR que sugiere el número del captcha en el chat; `OCR_URL` es solo el dominio |

El Worker usa: `GH_PAT` (dispara el workflow), `GIST_TOKEN` + `STATE_GIST_ID`
(lee la memoria), `TG_TOKEN` (bot de avisos), `TG_SECRET` (verifica el webhook).

## Cómo detecta cambios

La página de estado es ASP.NET WebForms: docenas de `<div>` de plantilla
ocultos y solo uno visible (`capaParaMostrar`). En vez de comparar el HTML
entero, `parse_status()` extrae unos pocos campos con sentido —estado,
detalle, fecha de solicitud, cita, resolución, notificaciones, panel activo—
y compara el hash de ese resumen. Sin falsos positivos por la fecha del día
ni el `VIEWSTATE`.

Para vigilar un dato nuevo (o callar uno ruidoso), edita `STATUS_SPANS` en
`monitor.py`. En local, cada respuesta cruda queda en `snapshots/`.

## Archivos

| archivo | qué es |
|---|---|
| `monitor.py` | el obrero (GitHub Actions) |
| `cloudflare/worker.js` · `wrangler.toml` | el Worker siempre vivo |
| `.github/workflows/monitor.yml` | el workflow que corre `monitor.py` |
| `docs/DEPLOY_ACTIONS.md` | guía de despliegue completa |
| `config.ini` · `state.json` · `snapshots/` | solo local, no se versionan |
| `maec-intermediate.pem` | CA intermedia FNMT que el servidor no envía en el handshake |

## Aviso

Uso personal para consultar **tu propio** trámite. El captcha lo resuelve una
persona y hay un intervalo mínimo entre consultas. No lo conviertas en un
scraper masivo del portal.
