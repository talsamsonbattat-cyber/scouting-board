/**
 * worker.js — Cloudflare Worker proxy for the Scouting Board's "Refresh Now".
 *
 * Holds your CFBD API key server-side so the static site never exposes it.
 *
 * Deploy (free tier):
 *   1. dash.cloudflare.com -> Workers & Pages -> Create Worker -> paste this file
 *   2. Settings -> Variables -> add SECRET  CFBD_API_KEY = <your key>
 *   3. (Recommended) set ALLOWED_ORIGIN below to your GitHub Pages URL,
 *      e.g. "https://yourname.github.io" — leaves anyone else locked out.
 *   4. Copy the worker URL into the site's "Refresh Now" dialog once.
 *
 * Only the read endpoints the site needs are allowed through.
 */

const ALLOWED_ORIGIN = "*"; // tighten to "https://yourname.github.io" after setup
const ALLOWED_PATHS = [
  /^\/teams\/fbs$/,
  /^\/roster$/,
  /^\/stats\/player\/season$/,
  /^\/games\/players$/,
];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    const cors = {
      "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "GET")
      return new Response("method not allowed", { status: 405, headers: cors });

    if (!ALLOWED_PATHS.some((re) => re.test(url.pathname)))
      return new Response("forbidden path", { status: 403, headers: cors });

    const target =
      "https://api.collegefootballdata.com" + url.pathname + url.search;
    const upstream = await fetch(target, {
      headers: { Authorization: "Bearer " + env.CFBD_API_KEY },
    });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { "content-type": "application/json", ...cors },
    });
  },
};
