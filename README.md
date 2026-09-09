# visa-monitor

Vigila el estado de un trámite consular en `sutramiteconsular.maec.es`
(vía "Acceso por Resguardo") y avisa por Telegram **solo cuando algo cambia**.

Corre en tu ordenador (Programador de tareas de Windows) o en la nube
(GitHub Actions, gratis) — ver [docs/DEPLOY_ACTIONS.md](docs/DEPLOY_ACTIONS.md).

## Cómo funciona (human-in-the-loop)

La vía del resguardo lleva un captcha numérico. **No se resuelve
automáticamente.** Cada consulta real al servidor la validas tú:

1. Un cron lanza `monitor.py` cada 30 min.
2. El script decide si "toca" (intervalo mínimo + horario activo). Si no toca,
   termina sin hacer nada.
3. Si toca: carga la web, descarga el captcha y te lo manda por Telegram.
4. Respondes con los 4–6 dígitos (o `skip` para saltar esa ronda).
5. El script envía el formulario, parsea la página de estado, la compara con
   la última guardada y:
   - primera vez → guarda la "línea base" y te la enseña;
   - si cambió → 🔔 te avisa con la hora exacta de detección, la hora de la
     revisión anterior (sin cambios) y qué campo cambió;
   - si no → **no dice nada**.

Ninguna petición al servidor va sin que tú hayas resuelto el captcha.

## Comandos (al bot de avisos, @Rocy_tramite_bot)

Los atiende el **Worker de Cloudflare** por webhook → respuesta **al instante**,
no dependen del cron.

| Comando | Efecto |
|---|---|
| `/estado` | Última **lectura guardada** y cuándo se tomó (hora de La Habana). Lee el gist, no toca la web. |
| `/historial` | Últimas ~20 revisiones con su hora y estado, marcando los cambios. |
| `/revisar` | Dispara `monitor.py` **ya** (independiente del cron): captcha al bot del captcha + resultado. Avisa aunque no haya cambios. |

Reparto de trabajo:
- **Worker** = siempre vivo; comandos + cron + memoria (gist).
- **monitor.py** = solo consulta el MAEC (captcha contigo) y **actualiza** el gist.

Aviso de resultado:
- revisión **automática** (cron) → solo si hay cambio;
- revisión **explícita** (`/revisar`, `--now`) → siempre, con "sin cambios".

El log de revisiones (últimas 120) vive en `state.json` / gist.

## Configuración

`monitor.py` lee **variables de entorno** primero (`BOT_TOKEN`, `CHAT_ID`,
`TRAMITE_ID`, `ANIO_NAC`, `TRAMITE_TIPO`, `MIN_INTERVAL_MINUTES`,
`ACTIVE_HOUR_START`, `ACTIVE_HOUR_END`, `CAPTCHA_REPLY_TIMEOUT_SECONDS`) y,
si no están, `config.ini`. El estado va a `state.json` local salvo que
pongas `STATE_GIST_ID` + `GIST_TOKEN` (gist privado, para la nube).

## Puesta en marcha (local)

```powershell
cd visa-monitor
py -3 -m pip install -r requirements.txt
copy config.example.ini config.ini
notepad config.ini   # token del bot, chat_id, datos del trámite
```

### Bot de Telegram

1. En Telegram habla con **@BotFather** → `/newbot` → copia el **token**.
2. Envía un "hola" a tu bot nuevo.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el número de
   `"chat":{"id": ...}` → ése es tu `chat_id`.

### Prueba manual

```powershell
py -3 monitor.py --now
```

`--now` salta el horario activo y el intervalo mínimo (para probar). Sin él,
el script solo consulta si "toca".

### Programar cada 30 min

```powershell
schtasks /create /tn "VisaMonitor" ^
  /tr "'%CD%\run.bat'" ^
  /sc minute /mo 30 /st 08:00 /f
```

Quitar: `schtasks /delete /tn "VisaMonitor" /f`

## Frecuencia

El estado de un visado cambia **como mucho una vez al día**, en horario de
oficina del consulado — así que chequear muy seguido no te da el dato antes,
solo te llena el móvil de captchas (cada consulta = 1 captcha que resuelves
tú). El deploy en Actions va configurado a cada 15 min, 24/7, por decisión
expresa del usuario; para algo más sensato, `MIN_INTERVAL_MINUTES` +
`ACTIVE_HOUR_START/END` en el workflow.

## Archivos

| archivo | qué es |
|---|---|
| `monitor.py` | el script |
| `.github/workflows/monitor.yml` | despliegue en GitHub Actions |
| `config.ini` | configuración local (no se versiona) |
| `state.json` | último hash/estado visto (no se versiona) |
| `snapshots/` | HTML crudo de cada respuesta, para depurar el parser |
| `monitor.log` | salida de las ejecuciones programadas |
| `maec-intermediate.pem` | CA intermedia FNMT que el servidor no envía |

## Cómo detecta cambios

La página de estado es ASP.NET WebForms: docenas de `<div>` de plantilla
ocultos y solo uno visible (`capaParaMostrar`). En vez de comparar el HTML
entero, `parse_status()` extrae unos pocos campos con sentido —estado,
detalle, fecha de solicitud, cita, resolución, notificaciones, panel
activo— y compara el hash de ese resumen. Así no hay falsos positivos por
la fecha del día ni por el `VIEWSTATE`.

Si aparece un dato nuevo que quieras vigilar (o uno que dé ruido), edita el
diccionario `STATUS_SPANS` de `monitor.py`. Cada respuesta cruda queda en
`snapshots/` para inspeccionarla.

## Aviso

Uso personal para consultar **tu propio** trámite. No resuelve el captcha
automáticamente y respeta un intervalo mínimo entre consultas. No lo
conviertas en un scraper masivo del portal.
