# visa-monitor

Vigila el estado de un trámite consular en `sutramiteconsular.maec.es`
(vía "Acceso por Resguardo") y avisa por Telegram **solo cuando algo cambia**.

## Cómo funciona (human-in-the-loop)

La vía del resguardo lleva un captcha numérico. **No se resuelve
automáticamente.** Cada consulta real al servidor la validas tú:

1. El Programador de tareas lanza `monitor.py` cada 30 min.
2. El script decide si "toca" (intervalo mínimo + horario activo). Si no toca,
   termina sin hacer nada.
3. Si toca: carga la web, descarga el captcha y te lo manda por Telegram.
4. Respondes con los 4–6 dígitos (o `skip` para saltar esa ronda).
5. El script envía el formulario, parsea la página de estado, la compara con
   la última guardada y:
   - primera vez → guarda la "línea base" y te la enseña;
   - si cambió → 🔔 te manda el texto nuevo;
   - si no → un "✓ sin cambios".

Ninguna petición al servidor va sin que tú hayas resuelto el captcha.

## Puesta en marcha

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

## Frecuencia — léelo

El estado de un visado cambia **como mucho una vez al día**, en horario de
oficina del consulado. Con `min_interval_minutes = 180` haces ~4 consultas
reales al día (4 captchas para ti). **No lo bajes de 120.** Pedir en bucle a
un servidor público de la Administración, aunque medie un captcha, no es
razonable ni te da el dato antes.

## Archivos

| archivo | qué es |
|---|---|
| `monitor.py` | el script |
| `config.ini` | tu configuración (no se versiona) |
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
