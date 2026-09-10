// Cloudflare Worker — cerebro "siempre vivo" del monitor de tramite consular.
//
// Responsabilidades:
//   1. Webhook de Telegram del bot de AVISOS (@Rocy_tramite_bot):
//        /start /help      -> responde al instante
//        /estado           -> lee el gist y responde al instante (sin captcha)
//        /historial        -> lee el gist y responde al instante
//        /revisar          -> dispara monitor.py YA (workflow_dispatch, force)
//   2. Cron (scheduled): dispara monitor.py cada 15 min para la vigilancia.
//   3. GET / : disparo manual de monitor.py (para pruebas).
//
// monitor.py ya NO maneja comandos: solo resuelve captcha, consulta el MAEC y
// actualiza estado + historial en el gist.
//
// Secrets (Worker -> Settings -> Variables and Secrets):
//   GH_PAT         PAT con Actions: Read and write sobre el repo
//   GIST_TOKEN     PAT classic con scope gist (lee el gist de estado)
//   STATE_GIST_ID  id del gist de estado
//   TG_TOKEN       token del bot de avisos (@Rocy_tramite_bot)
//   TG_SECRET      cadena aleatoria; debe coincidir con la del setWebhook

const REPO = "9804Charlie/tramite-consular-monitor";
const DISPATCH_URL =
  `https://api.github.com/repos/${REPO}/actions/workflows/monitor.yml/dispatches`;
const UA = "visa-monitor-worker";

const HELP = [
  "Bot activo ✅ — quedas suscrito a los avisos de cambio.",
  "",
  "/estado — ultima lectura guardada del tramite",
  "/historial — ultimas revisiones con su hora",
  "/revisar — fuerza una consulta real ahora (llega el captcha al otro bot)",
  "/baja — dejar de recibir avisos",
].join("\n");

// --- GitHub --------------------------------------------------------------- //

async function dispatch(env, force) {
  const body = force ? { ref: "main", inputs: { force: "true" } }
                     : { ref: "main" };
  const r = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": UA,
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) console.log(`dispatch -> ${r.status} ${await r.text()}`);
  return r.ok;
}

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GIST_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": UA,
  };
}

async function loadGistFile(env, name) {
  const r = await fetch(`https://api.github.com/gists/${env.STATE_GIST_ID}`,
    { headers: ghHeaders(env) });
  if (!r.ok) throw new Error(`gist ${r.status}`);
  const j = await r.json();
  const f = j.files && j.files[name];
  if (!f) return null;
  let content = f.content;
  if (f.truncated && f.raw_url) {
    content = await (await fetch(f.raw_url, { headers: ghHeaders(env) })).text();
  }
  try { return JSON.parse(content || "null"); } catch { return null; }
}

async function patchGistFile(env, name, obj) {
  const r = await fetch(`https://api.github.com/gists/${env.STATE_GIST_ID}`, {
    method: "PATCH",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body: JSON.stringify({
      files: { [name]: { content: JSON.stringify(obj, null, 2) } },
    }),
  });
  if (!r.ok) console.log(`patch ${name} -> ${r.status} ${await r.text()}`);
  return r.ok;
}

const loadState = (env) => loadGistFile(env, "state.json").then((v) => v || {});

async function loadSubs(env) {
  const v = await loadGistFile(env, "subscribers.json");
  return Array.isArray(v && v.chats) ? v.chats.map(String) : [];
}

async function subscribe(env, chatId, on) {
  const chats = new Set(await loadSubs(env));
  if (on) chats.add(String(chatId)); else chats.delete(String(chatId));
  await patchGistFile(env, "subscribers.json", { chats: [...chats] });
  return chats.size;
}

// --- Telegram ------------------------------------------------------------- //

async function tgSend(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text: text.slice(0, 4000),
      disable_web_page_preview: true,
    }),
  });
}

// --- Formato (espejo de monitor.py) ------------------------------------- //

function ts(epoch) {
  if (!epoch) return "desconocido";
  return new Intl.DateTimeFormat("es-ES", {
    timeZone: "America/Havana",
    day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(epoch * 1000)).replace(",", "");
}

const STATUS_PAIRS = [
  ["Situacion", "situacion_estado"], ["Resolucion", "resolucion"],
  ["Cita dia/hora", "cita_dia_hora"], ["Cita estado", "cita_estado"],
  ["Cita motivo", "cita_motivo"], ["Limite subsanar", "limite_subsanar"],
  ["Doc pendiente", "doc_pendiente"], ["Limite firmar", "limite_firmar"],
  ["Caducado", "caducado_estado"],
];

function formatStatus(d) {
  const L = [`Panel: ${d.panel || "?"}`];
  if (d.estado) {
    L.push(`Estado: ${d.estado}` +
      (d.estado_detalle ? ` — ${d.estado_detalle}` : ""));
  }
  if (d.tramite) {
    L.push(`Tramite: ${d.tramite} (solicitud ${d.fecha_solicitud || "?"})`);
  }
  for (const [label, key] of STATUS_PAIRS) if (d[key]) L.push(`${label}: ${d[key]}`);
  const notis = (d.notificaciones || [])
    .filter((n) => n !== "NO TIENE NOTIFICACIONES");
  L.push("Notificaciones: " + (notis.length ? notis.join("; ") : "(ninguna)"));
  return L.join("\n");
}

function statusReport(state) {
  const d = state.status_data;
  if (!d) return "Aun no hay una linea base capturada.";
  return `Estado (ultima lectura ${ts(state.last_ok_ts)}):\n\n` + formatStatus(d);
}

function historyReport(state) {
  const h = state.history || [];
  if (!h.length) return "Sin historial todavia.";
  const lines = [`Historial (${h.length} guardadas, ultimas 20):`];
  for (const e of h.slice(-20)) {
    lines.push(`${ts(e.ts)}  ${e.estado || "?"}` +
      (e.detalle ? ` / ${e.detalle}` : "") +
      (e.cambio ? "  <<< CAMBIO" : ""));
  }
  return lines.join("\n");
}

// --- Router ------------------------------------------------------------- //

async function handleUpdate(env, update) {
  const msg = update.message || update.edited_message;
  if (!msg || !msg.text) return;
  const chatId = msg.chat.id;
  const cmd = msg.text.trim().toLowerCase().split(/\s+/)[0].split("@")[0];

  if (["/start", "/suscribir"].includes(cmd)) {
    let extra = "";
    try { await subscribe(env, chatId, true); }
    catch (e) { extra = "\n\n⚠️ no pude guardar la suscripcion: " + e.message; }
    await tgSend(env, chatId, HELP + extra);
  } else if (["/help", "/ayuda"].includes(cmd)) {
    await tgSend(env, chatId, HELP);
  } else if (["/baja", "/stop"].includes(cmd)) {
    try {
      await subscribe(env, chatId, false);
      await tgSend(env, chatId, "Hecho, ya no recibes avisos. /start para volver.");
    } catch (e) {
      await tgSend(env, chatId, "No pude darte de baja: " + e.message);
    }
  } else if (cmd === "/id") {
    await tgSend(env, chatId, `${chatId}`);
  } else if (cmd === "/estado") {
    try { await tgSend(env, chatId, statusReport(await loadState(env))); }
    catch (e) { await tgSend(env, chatId, "No pude leer el estado: " + e.message); }
  } else if (cmd === "/historial") {
    try { await tgSend(env, chatId, historyReport(await loadState(env))); }
    catch (e) { await tgSend(env, chatId, "No pude leer el historial: " + e.message); }
  } else if (cmd === "/revisar") {
    const ok = await dispatch(env, true);
    await tgSend(env, chatId, ok
      ? "🔄 Lanzando revision. El captcha te llega al bot @Revisor98bot."
      : "No pude lanzar la revision (ver logs del Worker).");
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env, false));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/tg") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TG_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      try {
        await handleUpdate(env, await request.json());
        return new Response("ok");
      } catch (e) {
        console.log("update error: " + e.message);
        return new Response("error", { status: 500 });
      }
    }

    if (url.pathname === "/") {
      const ok = await dispatch(env, false);
      return new Response(ok ? "dispatched\n" : "failed (ver logs)\n",
        { status: ok ? 200 : 502 });
    }

    return new Response("not found", { status: 404 });
  },
};
