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

`Actions` → workflow **monitor** → `Run workflow`. En ~1 min te llega el
captcha al bot; respóndelo y mira que el job termine en verde.

## 5. Apagar el de tu ordenador

```powershell
Unregister-ScheduledTask -TaskName VisaMonitor -Confirm:$false
```

## Notas

- **Cron perezoso**: GitHub puede retrasar los disparos 5–15 min en horas
  punta. Irrelevante aquí.
- **Repo inactivo**: si no hay commits en ~60 días, GitHub deshabilita los
  workflows programados (te avisa por email). Un commit cualquiera lo
  reactiva.
- **Concurrencia**: `concurrency` evita que dos ejecuciones se pisen
  hablando con Telegram a la vez.
- Local sigue funcionando igual con `config.ini` (sin variables de entorno).
