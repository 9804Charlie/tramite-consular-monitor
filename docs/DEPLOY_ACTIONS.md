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

## 5. El cron (cron-job.org)

El `schedule:` de GitHub Actions es poco fiable con intervalos cortos, así
que el disparo cada 15 min lo hace un servicio externo gratis que llama a la
API de `workflow_dispatch`.

### 5a. PAT para disparar el workflow

- <https://github.com/settings/personal-access-tokens/new> → **fine-grained**
- Repository access: **Only select repositories** → `tramite-consular-monitor`
- Permissions → Repository permissions → **Actions: Read and write**
- Genera y copia el `github_pat_...`.

### 5b. cron-job.org

- Crea cuenta en <https://console.cron-job.org> → **Create cronjob**
- **URL**:
  `https://api.github.com/repos/9804Charlie/tramite-consular-monitor/actions/workflows/monitor.yml/dispatches`
- **Schedule**: every 15 minutes
- **Advanced** → Request method: **POST**
- **Headers**:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer github_pat_...`
  - `X-GitHub-Api-Version: 2022-11-28`
- **Body**: `{"ref":"main"}`
- Guarda. "Test run" debe devolver **204**.

Cada 15 min cron-job.org dispara el workflow; el `MIN_INTERVAL_MINUTES=12`
del workflow evita dobles consultas si algún disparo se adelanta.

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
