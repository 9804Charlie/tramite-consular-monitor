# Correr en GitHub Actions (sin tu ordenador)

El workflow `.github/workflows/monitor.yml` ejecuta `monitor.py` en un cron
sobre los runners de GitHub. Sigue siendo human-in-the-loop: cuando toca
consulta, te llega el captcha por Telegram y respondes desde el móvil.

Para repos públicos los minutos de Actions son gratis e ilimitados.

## 1. Token para el gist (estado entre ejecuciones)

El runner nace limpio cada vez, así que `state.json` se guarda en un **gist
privado**. Necesitas un token que pueda escribirlo:

- <https://github.com/settings/tokens/new> → **Generate new token (classic)**
- Nota: `visa-monitor gist`, Expiración: la que quieras, marca **solo** el
  scope `gist`. Genera y copia el `ghp_...`.

## 2. El gist

- <https://gist.github.com> → crea un gist con:
  - nombre de archivo: `state.json`
  - contenido: el `state.json` que ya tienes (para conservar la línea base)
    o `{}` si quieres empezar de cero.
- **Create secret gist**.
- Copia el ID del gist: es el trozo de la URL después de tu usuario
  (`https://gist.github.com/<user>/<ESTE_ID>`).

## 3. Secrets del repo

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`:

| Secret | Valor |
|---|---|
| `BOT_TOKEN` | token del bot de Telegram |
| `CHAT_ID` | tu chat id |
| `TRAMITE_ID` | el identificador del resguardo |
| `ANIO_NAC` | año de nacimiento |
| `STATE_GIST_ID` | ID del gist del paso 2 |
| `GIST_TOKEN` | el `ghp_...` del paso 1 |
| `NOTIFY_BOT_TOKEN` | *(opcional)* token de otro bot solo para los avisos de resultado |
| `NOTIFY_CHAT_ID` | *(opcional)* chat id de ese otro bot |

Si no pones `NOTIFY_*`, los avisos de resultado van al mismo bot del captcha.

Comandos que puedes mandar a cualquiera de los dos bots: `/estado` (última
lectura guardada, sin captcha), `/historial` (últimas revisiones con su
hora) y `/revisar` (fuerza consulta en la próxima pasada del cron; avisa
aunque no haya cambios). Se procesan cuando el cron despierta al script.

(El tipo de trámite va fijo a `VISADO` en el workflow; cámbialo allí si hace
falta. `MIN_INTERVAL_MINUTES` y el horario activo también se ajustan en el
workflow.)

## 4. Probar

`Actions` → workflow **monitor** → `Run workflow`, marca **force** (ignora
el horario y el intervalo mínimo) → `Run`. En ~1 min te llega el captcha al
bot; respóndelo y mira que el job termine en verde.

Por CLI:

```powershell
gh workflow run monitor --repo 9804Charlie/tramite-consular-monitor -f force=true
```

## 5. El Worker de Cloudflare (cerebro siempre vivo)

`cloudflare/worker.js` hace tres cosas:
- **webhook de Telegram** del bot de avisos → `/start` `/estado` `/historial`
  `/revisar` responden al instante (leen el gist; `/revisar` dispara el
  workflow con force);
- **cron** (`scheduled`) → dispara el workflow cada 15 min;
- **GET /** → disparo manual, para pruebas.

### 5a. Secrets del Worker

`dash.cloudflare.com` → Workers & Pages → tu Worker → **Settings** →
**Variables and Secrets** → añade como **Secret**:

| Secret | Valor |
|---|---|
| `GH_PAT` | PAT con **Actions: Read and write** sobre el repo (fine-grained sirve) |
| `GIST_TOKEN` | PAT **classic** con scope `gist` (lee el gist de estado) |
| `STATE_GIST_ID` | el id del gist de estado |
| `TG_TOKEN` | token del bot de avisos (`@Rocy_tramite_bot`) |
| `TG_SECRET` | una cadena aleatoria (la misma que uses en el setWebhook) |

Pega `cloudflare/worker.js` en el editor del Worker → **Deploy**.

### 5b. Cron Trigger

Worker → Settings → **Cron Triggers** → `*/15 * * * *`.
Si el panel no te deja, usa **cron-job.org** apuntando a
`https://<tu-worker>.workers.dev/` (GET, cada 15 min).

### 5c. Webhook de Telegram

Una vez desplegado el Worker, registra el webhook del bot de avisos:

```
curl "https://api.telegram.org/bot<TG_TOKEN>/setWebhook?url=https://<tu-worker>.workers.dev/tg&secret_token=<TG_SECRET>"
```

(Solo el bot de avisos lleva webhook. El bot del captcha sigue con polling
porque `monitor.py` necesita `getUpdates` para leer los dígitos.)

### 5d. Prueba

- `/estado` al bot de avisos → responde al instante con la última lectura.
- `/revisar` → "Lanzando revisión" + en ~1 min llega el captcha al bot del
  captcha.

El `MIN_INTERVAL_MINUTES=12` del workflow evita dobles consultas.

## 6. Apagar el de tu ordenador

```powershell
Unregister-ScheduledTask -TaskName VisaMonitor -Confirm:$false
```

## Notas

- **Repo inactivo**: `workflow_dispatch` por API no caduca por inactividad
  (a diferencia de `schedule:`).
- **Concurrencia**: `concurrency` + `cancel-in-progress` evita que dos
  ejecuciones hablen con Telegram a la vez.
- Local sigue funcionando igual con `config.ini` (sin variables de entorno).
