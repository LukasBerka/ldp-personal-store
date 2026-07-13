/* client.js — connection state, the single HTTP entry point, and the request log.
 *
 * Every request in the app funnels through Pod.http(), which attaches the bearer
 * token, times the round trip, splits textual vs binary responses, records a log
 * entry (never storing the token), and returns a normalized result object. */

'use strict';

const Pod = (function () {
  const KEYS = { base: 'ldpc.base', admin: 'ldpc.admin', consumer: 'ldpc.consumer', role: 'ldpc.role' };
  const DEFAULT_BASE = 'http://localhost:8000';

  const log = [];
  const logListeners = [];

  // ---- persisted connection state -----------------------------------------
  const get = (k, d) => localStorage.getItem(k) ?? d;
  const set = (k, v) => localStorage.setItem(k, v);

  const origin = () => (get(KEYS.base, DEFAULT_BASE) || DEFAULT_BASE).trim().replace(/\/+$/, '');
  const adminToken = () => (get(KEYS.admin, '') || '').trim();
  const consumerToken = () => (get(KEYS.consumer, '') || '').trim();
  const role = () => get(KEYS.role, 'owner');

  const setBase = (v) => set(KEYS.base, v);
  const setAdmin = (v) => set(KEYS.admin, v);
  const setConsumer = (v) => set(KEYS.consumer, v);
  const setRole = (v) => set(KEYS.role, v);

  // Reserved-prefix URI builders, anchored at the current base.
  const url = (path) => origin() + '/' + String(path).replace(/^\/+/, '');
  const ns = {
    viewsContainer: () => origin() + '/.system/views/',
    view: (id) => origin() + '/.system/views/' + id,
    tokensContainer: () => origin() + '/.system/tokens/',
    token: (id) => origin() + '/.system/tokens/' + id,
    policy: (id) => origin() + '/.system/tokens/policies/' + id,
    discovery: () => origin() + '/.engine/discovery',
    engineView: (id) => origin() + '/.engine/views/' + id,
    engineViewsBase: () => origin() + '/.engine/views/',
    stats: () => origin() + '/.engine/stats',
  };

  // ---- textual vs binary detection ----------------------------------------
  const TEXTUAL = /^(text\/|application\/(json|ld\+json|xml|rdf\+xml|n-triples|n-quads|trig|sparql-results\+json|sparql-results\+xml|sparql-query))|turtle|csv/i;
  const isTextual = (ct) => !ct || TEXTUAL.test(ct);

  // ---- request log ---------------------------------------------------------
  function pushLog(entry) {
    log.unshift(entry);
    if (log.length > 100) log.pop();
    logListeners.forEach((cb) => cb(log));
  }
  const onLog = (cb) => { logListeners.push(cb); cb(log); };
  const clearLog = () => { log.length = 0; logListeners.forEach((cb) => cb(log)); };

  // ---- the one fetch() -----------------------------------------------------
  async function http({ method, url: u, token, headers = {}, body = null }) {
    const h = new Headers(headers);
    if (token) h.set('Authorization', 'Bearer ' + token);

    const entry = {
      method, url: u,
      reqHeaders: [...h].filter(([k]) => k.toLowerCase() !== 'authorization'),
      reqBody: typeof body === 'string' ? body : (body instanceof File ? `[file ${body.name}, ${body.size} bytes]` : null),
      status: 0, statusText: '', ms: 0, ok: false,
    };

    const started = performance.now();
    let res = null, netErr = null;
    try { res = await fetch(u, { method, headers: h, body }); }
    catch (e) { netErr = e; }
    const ms = Math.round(performance.now() - started);

    if (netErr) {
      Object.assign(entry, { status: 0, statusText: 'network error', ms, error: netErr.message });
      pushLog(entry);
      return { ok: false, network: true, status: 0, statusText: 'network error', ms, error: netErr.message, url: u, method };
    }

    const ct = res.headers.get('content-type') || '';
    let text = null, blob = null;
    if (method === 'HEAD') { /* no body */ }
    else if (isTextual(ct)) text = await res.text();
    else blob = await res.blob();

    Object.assign(entry, {
      status: res.status, statusText: res.statusText, ms, ok: res.ok, contentType: ct,
      respHeaders: [...res.headers],
      respPreview: text != null ? text.slice(0, 4000) : (blob ? `[binary · ${ct || 'unknown'} · ${blob.size} bytes]` : ''),
    });
    pushLog(entry);

    return {
      ok: res.ok, status: res.status, statusText: res.statusText, ms, url: u, method,
      contentType: ct, headers: res.headers, text, blob,
      location: res.headers.get('location'),
      etag: res.headers.get('etag'),
    };
  }

  // Convenience wrappers that pick the right token for the role.
  const asAdmin = (opts) => http({ ...opts, token: adminToken() });
  const asConsumer = (opts) => http({ ...opts, token: consumerToken() });

  return {
    KEYS, DEFAULT_BASE,
    origin, adminToken, consumerToken, role,
    setBase, setAdmin, setConsumer, setRole,
    url, ns, isTextual,
    http, asAdmin, asConsumer,
    onLog, clearLog,
  };
})();

window.Pod = Pod;
