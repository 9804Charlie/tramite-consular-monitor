// Cloudflare Worker: cada 15 min dispara el workflow "monitor" en GitHub
// via workflow_dispatch. Sustituye a cron-job.org.
//
// Deploy (dashboard): Workers & Pages -> Create Worker -> pega esto ->
//   Settings -> Variables and Secrets -> add secret GH_PAT (PAT fine-grained
//     con permiso Actions: Read and write sobre el repo)
//   Settings -> Trigger Events -> Cron Triggers -> "*/15 * * * *"
// Deploy (wrangler): `npx wrangler deploy` con el wrangler.toml de al lado,
//   y `npx wrangler secret put GH_PAT`.

const DISPATCH_URL =
  "https://api.github.com/repos/9804Charlie/tramite-consular-monitor" +
  "/actions/workflows/monitor.yml/dispatches";

async function trigger(env) {
  const r = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_PAT}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "cf-worker-visa-monitor", // GitHub lo exige
    },
    body: JSON.stringify({ ref: "main" }),
  });
  const body = r.ok ? "" : ` ${await r.text()}`;
  console.log(`workflow_dispatch -> ${r.status}${body}`);
  return r.ok;
}

export default {
  // disparo programado
  async scheduled(event, env, ctx) {
    ctx.waitUntil(trigger(env));
  },
  // GET manual para probar: https://<worker>.workers.dev/
  async fetch(request, env) {
    const ok = await trigger(env);
    return new Response(ok ? "dispatched\n" : "failed (ver logs)\n", {
      status: ok ? 200 : 502,
    });
  },
};
