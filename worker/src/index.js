// NewsDesk API (Cloudflare Worker): реле за блокирани източници, вход с Google и синхронизация между устройства.
const enc = new TextEncoder();
const UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,}$/;
const HEXRE = /^[0-9a-f]{8,16}$/;

const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
const sha256 = async (s) => hex(await crypto.subtle.digest("SHA-256", enc.encode(String(s))));
const randHex = (n) => hex(crypto.getRandomValues(new Uint8Array(n)));

async function same(a, b) {  // сравнение на тайни без изтичане по времето
  const [x, y] = await Promise.all([crypto.subtle.digest("SHA-256", enc.encode(a)), crypto.subtle.digest("SHA-256", enc.encode(b))]);
  return crypto.subtle.timingSafeEqual(x, y);
}

function cors(req, env) {
  const list = String(env.ALLOW_ORIGIN || "").split(",").map((s) => s.trim()).filter(Boolean);
  const o = req.headers.get("Origin") || "";
  return {
    "Access-Control-Allow-Origin": list.includes(o) ? o : list[0] || "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Relay-Key, X-UA",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Vary": "Origin",
  };
}

const json = (req, env, obj, code = 200) =>
  new Response(JSON.stringify(obj), { status: code, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...cors(req, env) } });

// ---- реле: GitHub взима през него източници, които блокират неговите адреси ----
async function relay(req, env, url) {
  if (!env.RELAY_KEY || !(await same(req.headers.get("X-Relay-Key") || "", env.RELAY_KEY))) return json(req, env, { error: "forbidden" }, 403);
  const hosts = String(env.RELAY_HOSTS || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const okHost = (u) => (u.protocol === "https:" || u.protocol === "http:") && (!u.port || u.port === "80" || u.port === "443") &&
    hosts.some((h) => u.hostname.toLowerCase() === h || u.hostname.toLowerCase().endsWith("." + h));
  let u;
  try { u = new URL(url.searchParams.get("u") || ""); } catch { return json(req, env, { error: "bad url" }, 400); }
  const xua = req.headers.get("X-UA") || "";
  const ua = xua && xua.length < 200 && !/[\r\n]/.test(xua) ? xua : UA;  // някои сайтове (ESPN) пускат само определен User-Agent
  for (let hop = 0; hop < 4; hop++) {
    if (!okHost(u)) return json(req, env, { error: "host not allowed" }, 403);
    const r = await fetch(u.toString(), { redirect: "manual", headers: { "User-Agent": ua, Accept: "application/rss+xml, application/xml, text/xml, */*", "Accept-Language": "en-US,en;q=0.9" } });
    if (r.status >= 300 && r.status < 400 && r.headers.get("Location")) {
      try { u = new URL(r.headers.get("Location"), u); } catch { return json(req, env, { error: "bad redirect" }, 502); }
      continue;
    }
    const buf = await r.arrayBuffer();
    if (buf.byteLength > 3_000_000) return json(req, env, { error: "too large" }, 413);
    return new Response(buf, { status: r.status, headers: { "Content-Type": r.headers.get("Content-Type") || "text/plain", "Cache-Control": "no-store", ...cors(req, env) } });
  }
  return json(req, env, { error: "too many redirects" }, 502);
}

// ---- акаунти: Google вход, сесии, синхронизация ----
let schema;
const db = async (env) => { schema ||= env.DB.exec("CREATE TABLE IF NOT EXISTS users (uid TEXT PRIMARY KEY, doc TEXT NOT NULL, updated INTEGER)"); await schema; return env.DB; };
async function getDoc(env, uid) { const r = await (await db(env)).prepare("SELECT doc FROM users WHERE uid=?").bind(uid).first(); return r ? JSON.parse(r.doc) : null; }
async function putDoc(env, uid, doc) {
  await (await db(env)).prepare("INSERT INTO users(uid,doc,updated) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET doc=excluded.doc, updated=excluded.updated").bind(uid, JSON.stringify(doc), Date.now()).run();
}

async function uidOf(env, email) {
  const e = String(email).trim().toLowerCase();
  return EMAIL_RE.test(e) ? (await sha256((env.USER_SALT || "nd") + e)).slice(0, 32) : null;
}

export async function verifyGoogle(env, credential, now = Date.now() / 1000) {
  const base = env.VERIFY_URL || "https://oauth2.googleapis.com/tokeninfo";
  const r = await fetch(base + "?id_token=" + encodeURIComponent(String(credential).slice(0, 4000)));
  if (!r.ok) throw new Error("tokeninfo " + r.status);
  const t = await r.json();
  if (t.aud !== env.GOOGLE_CLIENT_ID || !["accounts.google.com", "https://accounts.google.com"].includes(t.iss)) throw new Error("aud/iss");
  if (String(t.email_verified).toLowerCase() !== "true" || Number(t.exp || 0) < now) throw new Error("email/exp");
  return String(t.email).trim().toLowerCase();
}

async function newSession(env, uid) {
  const token = randHex(24);
  const doc = (await getDoc(env, uid)) || {};
  doc.sess = (doc.sess || []).slice(-9).concat([await sha256(token)]);
  await putDoc(env, uid, doc);
  return [token, doc];
}

function cleanMon(m) {
  if (!m || typeof m !== "object" || typeof m.id !== "string" || !/^[\w-]{1,16}$/.test(m.id)) return null;
  return { id: m.id, name: String(m.name ?? "").slice(0, 60), q: String(m.q ?? "").slice(0, 300), scope: m.scope === "all" ? "all" : "title",
    period: ["6h", "24h"].includes(m.period) ? m.period : "all", notify: !!m.notify, u: Math.trunc(Number(m.u) || 0) };
}

export function syncMerge(old, nw, now = Date.now() / 1000) {
  old = old || {}; nw = nw || {};
  const mon = new Map();
  for (const raw of [...(old.mon || []), ...(nw.mon || [])]) {
    const m = cleanMon(raw);
    if (m && m.u >= (mon.get(m.id)?.u ?? -1)) mon.set(m.id, m);
  }
  const tomb = {};
  for (const src of [old.tomb || {}, nw.tomb || {}]) {
    for (const [k, v] of Object.entries(src).slice(0, 300)) if (typeof v === "number") tomb[k] = Math.max(tomb[k] || 0, Math.trunc(v));
  }
  const monList = [...mon.values()].filter((m) => m.u > (tomb[m.id] || 0)).slice(0, 50);
  const rh = [...(old.readH || []), ...(nw.readH || [])].filter((h) => typeof h === "string" && HEXRE.test(h));
  const po = old.prefs || {}, pn = nw.prefs || {};
  const pf = (pn.u || 0) >= (po.u || 0) ? pn : po;
  return {
    mon: monList,
    tomb: Object.fromEntries(Object.entries(tomb).sort((a, b) => b[1] - a[1]).slice(0, 200)),
    readH: [...new Set(rh)].slice(-6000),
    prefs: { u: Math.trunc(pf.u || 0), bg: pf.bg === undefined ? true : !!pf.bg, mix: pf.mix === undefined ? true : !!pf.mix, theme: ["light", "dark"].includes(pf.theme) ? pf.theme : "" },
    updated: now,
  };
}

const strip = (d) => JSON.stringify(Object.fromEntries(Object.entries(d).filter(([k]) => k !== "updated" && k !== "sess").sort()), (k, v) =>
  v && typeof v === "object" && !Array.isArray(v) ? Object.fromEntries(Object.entries(v).sort()) : v);
const pub = (doc) => Object.fromEntries(Object.entries(doc).filter(([k]) => k !== "sess"));

async function account(req, env, action, body) {
  if (action === "login_start" || action === "login_verify") return json(req, env, { error: "Входът по имейл още не е настроен" }, 503);
  if (action === "login_google") {
    let email;
    try { email = await verifyGoogle(env, body.credential || ""); } catch { return json(req, env, { error: "Google не потвърди входа, опитай пак" }, 401); }
    const uid = await uidOf(env, email);
    if (!uid) return json(req, env, { error: "не успях да запиша" }, 500);
    const [token, doc] = await newSession(env, uid);
    return json(req, env, { ok: true, uid, email, token, ...pub(doc) });
  }
  const uid = String(body.uid || "");
  if (!/^[0-9a-f]{32}$/.test(uid)) return json(req, env, { error: "неправилен вход" }, 400);
  const doc = await getDoc(env, uid);
  const th = await sha256(body.token || "");
  if (!doc || !(doc.sess || []).includes(th)) return json(req, env, { error: "сесията е изтекла" }, 401);
  if (action === "logout") {
    doc.sess = doc.sess.filter((x) => x !== th);
    await putDoc(env, uid, doc);
    return json(req, env, { ok: true });
  }
  const merged = syncMerge(doc, body.data && typeof body.data === "object" ? body.data : {});
  if (strip(merged) !== strip(doc)) await putDoc(env, uid, { ...merged, sess: doc.sess });
  return json(req, env, merged);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req, env) });
    try {
      if (url.pathname === "/relay") return await relay(req, env, url);
      if (url.pathname === "/api/config" || (url.pathname === "/api/items" && url.searchParams.get("action") === "config")) {
        return json(req, env, { google: env.GOOGLE_CLIENT_ID, email: false, admin_required: false, readonly: true });
      }
      if (req.method === "POST" && url.pathname === "/api/items") {
        const action = url.searchParams.get("action") || "";
        if (!["login_start", "login_verify", "login_google", "sync", "logout"].includes(action)) return json(req, env, { error: "непознато действие" }, 400);
        const text = await req.text();
        if (text.length > (action === "sync" ? 300000 : 4000)) return json(req, env, { error: "твърде голяма заявка" }, 413);
        let body;
        try { body = JSON.parse(text || "{}"); } catch { return json(req, env, { error: "грешна заявка" }, 400); }
        return await account(req, env, action, body);
      }
      return json(req, env, { error: "not found" }, 404);
    } catch (ex) {
      console.log("worker error:", String(ex).slice(0, 200));
      return json(req, env, { error: "грешка на сървъра" }, 500);
    }
  },
};
