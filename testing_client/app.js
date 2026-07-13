/* app.js — UI wiring for the Test Console.
 *
 * Owner mode drives the setup surface (data, views, grants, policies, SPARQL,
 * stats); Consumer mode drives the read surface (discovery + view/blob fetch).
 * All RDF reads request N-Triples and go through RDF.parseNTriples; writes send
 * Turtle built by rdf.js. Every request goes through Pod.http (see client.js). */

'use strict';

// ------------------------------------------------------------------ DOM utils
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    n.appendChild(typeof kid === 'string' ? document.createTextNode(kid) : kid);
  }
  return n;
}
const clear = (n) => { n.innerHTML = ''; return n; };

function toast(msg, kind) {
  const t = el('div', { class: 'toast' + (kind ? ' ' + kind : ''), text: msg });
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), 2600);
}
async function copy(text) {
  if (!text) return;
  try { await navigator.clipboard.writeText(text); toast('Copied', 'ok'); }
  catch (e) { toast('Copy failed — select and copy manually', 'err'); }
}
function flash(node) { node.classList.add('flash'); setTimeout(() => node.classList.remove('flash'), 1000); }

// ------------------------------------------------------------------ rendering
function statusHead(res) {
  const cls = 'status status-' + String(res.status)[0];
  const head = el('div', { class: 'resp-head' },
    el('span', { class: cls, text: res.network ? 'network error' : (res.status + ' ' + res.statusText) }),
    el('span', { class: 'kv', text: res.ms + ' ms' }));
  if (res.contentType) head.appendChild(el('span', { class: 'kv', text: res.contentType }));
  if (res.etag) head.appendChild(el('span', { class: 'kv', text: 'ETag ' + res.etag }));
  if (res.location) head.appendChild(el('span', { class: 'kv', text: 'Location ' + res.location }));
  if (res.url) head.appendChild(el('span', { class: 'kv', style: 'flex-basis:100%;word-break:break-all', text: (res.method ? res.method + ' ' : '') + res.url }));
  return head;
}
const notice = (msg, kind) => el('div', { class: 'notice ' + (kind || 'info'), text: msg });

function codeBlock(text, label) {
  return el('div', {},
    el('div', { class: 'row', style: 'margin-bottom:.2rem;gap:.4rem' },
      label ? el('span', { class: 'kv', text: label }) : null,
      el('span', { class: 'grow' }),
      el('button', { class: 'btn ghost small', onclick: () => copy(text) }, 'Copy')),
    el('pre', { class: 'code', text: text }));
}

function errorDetail(res) {
  if (res.network) return 'Network error reaching ' + (res.url || 'the pod') + ' — is the pod running at that exact host/port, and is CORS enabled?';
  let detail = '';
  if (res.text && /json/i.test(res.contentType || '')) {
    try { const j = JSON.parse(res.text); if (j.detail != null) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (e) { /* ignore */ }
  }
  const generic = {
    400: 'Bad request — unparsable RDF or SPARQL.',
    401: 'Unauthorized — check the token for this role (all auth failures look identical by design).',
    403: 'Forbidden — outside the grant’s scope, a reserved prefix, or a policy denied it.',
    404: 'Not found.',
    405: 'Method not allowed.',
    409: 'Conflict — server-managed containment, or a non-empty container.',
    412: 'Precondition failed — the ETag no longer matches.',
    415: 'Unsupported media type.',
    422: 'Unprocessable — the shape is invalid (detail explains).',
    428: 'Replace requires an If-Match ETag.',
    500: 'Server error (500) — the pod hit an unhandled exception; check the pod’s terminal for the traceback.',
    502: 'The engine could not reach storage (its credential may be revoked).',
  }[res.status];
  return detail || generic || (res.status + ' ' + res.statusText);
}

// Standard render: head + error notice, or a success note + body.
function renderResponse(target, res, opts = {}) {
  clear(target);
  target.appendChild(statusHead(res));
  if (res.network || !res.ok) {
    target.appendChild(notice(errorDetail(res), 'err'));
    if (res.text) target.appendChild(codeBlock(res.text));
    return;
  }
  if (opts.successNote) target.appendChild(notice(opts.successNote, 'ok'));
  if (res.text != null && res.text !== '') target.appendChild(codeBlock(res.text));
  else if (res.blob) target.appendChild(binaryView(res));
}

// ------------------------------------------------------------------ binaries
const humanSize = (b) => b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(1) + ' KB' : (b / 1048576).toFixed(1) + ' MB';
function extFor(ct) {
  ct = (ct || '').split(';')[0].trim();
  return { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/svg+xml': 'svg', 'image/webp': 'webp', 'application/pdf': 'pdf', 'text/plain': 'txt', 'text/turtle': 'ttl', 'application/json': 'json', 'application/ld+json': 'jsonld', 'text/csv': 'csv', 'application/n-triples': 'nt', 'application/rdf+xml': 'rdf' }[ct] || '';
}
function filenameForUrl(u, ct) {
  try {
    const url = new URL(u, Pod.origin());
    let src = url.searchParams.get('uri') || url.pathname;
    src = src.replace(/\/+$/, '');
    let name = decodeURIComponent(src.split('/').pop() || 'download');
    if (!/\.[a-z0-9]{1,6}$/i.test(name)) { const e = extFor(ct); if (e) name += '.' + e; }
    return name || 'download';
  } catch (e) { return 'download'; }
}
function triggerDownload(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = el('a', { href: url, download: name });
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
function binaryView(res, nameHint) {
  const wrap = el('div');
  const url = URL.createObjectURL(res.blob);
  const ext = extFor(res.contentType);
  const name = nameHint || ('download' + (ext ? '.' + ext : ''));
  if (/^image\//.test(res.contentType || '')) wrap.appendChild(el('img', { class: 'preview', src: url, alt: name }));
  wrap.appendChild(el('div', { class: 'row' },
    el('a', { class: 'btn primary small', href: url, download: name }, 'Download ' + name),
    el('span', { class: 'kv', text: humanSize(res.blob.size) })));
  return wrap;
}

// ------------------------------------------------------------------ role/tabs
function applyRole(r) {
  Pod.setRole(r);
  $$('[data-role]').forEach((b) => { const on = b.dataset.role === r; b.classList.toggle('active', on); b.setAttribute('aria-selected', on); });
  $$('[data-role-show]').forEach((n) => { n.hidden = n.dataset.roleShow !== r; });
  const nav = r === 'owner' ? $('#ownerTabs') : $('#consumerTabs');
  const active = nav.querySelector('.tab.active') || nav.querySelector('.tab');
  activateTab(active.dataset.tab, r);
}
function activateTab(tab, r) {
  r = r || Pod.role();
  const nav = r === 'owner' ? $('#ownerTabs') : $('#consumerTabs');
  $$('.tab', nav).forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.panel').forEach((p) => { if (p.dataset.roleShow === r) p.classList.toggle('active', p.dataset.panel === tab); });
}
function showTab(tab, role) { if (role && Pod.role() !== role) applyRole(role); activateTab(tab, role); }

// ------------------------------------------------------------------ health
async function checkHealth() {
  const pill = $('#healthPill');
  pill.className = 'pill neutral'; pill.textContent = 'checking…';
  const res = await Pod.http({ method: 'GET', url: Pod.origin() + '/health', headers: { Accept: 'application/json' } });
  if (res.ok) {
    let v = ''; try { v = JSON.parse(res.text).version; } catch (e) { /* ignore */ }
    pill.className = 'pill ok'; pill.textContent = 'ok' + (v ? ' · v' + v : '');
  } else {
    pill.className = 'pill err';
    pill.textContent = res.network ? 'unreachable' : String(res.status);
  }
}

// ------------------------------------------------------------------ param rows
function paramRow(container, values = {}) {
  const nameI = el('input', { class: 'grow', spellcheck: 'false', placeholder: 'name', value: values.name || '' });
  const typeS = el('select');
  for (const t of ['str', 'int', 'iri', 'date', 'dateTime']) typeS.appendChild(el('option', { value: t, selected: values.type === t }, t));
  const row = el('div', { class: 'param-row' }, nameI, typeS,
    el('button', { class: 'mini', onclick: () => row.remove() }, '✕'));
  row._get = () => ({ name: nameI.value.trim(), type: typeS.value });
  container.appendChild(row);
  return row;
}
const collectParams = (container) => $$('.param-row', container).map((r) => r._get()).filter((p) => p.name);

function kvRow(container, k = '', v = '') {
  const kI = el('input', { class: 'k grow', spellcheck: 'false', placeholder: 'name', value: k });
  const vI = el('input', { class: 'v grow', spellcheck: 'false', placeholder: 'value', value: v });
  const row = el('div', { class: 'param-row' }, kI, vI, el('button', { class: 'mini', onclick: () => row.remove() }, '✕'));
  container.appendChild(row);
  return row;
}

function typedParamInput(p) {
  const type = p.type || 'str';
  let input;
  if (type === 'int') input = el('input', { type: 'number', placeholder: 'integer' });
  else if (type === 'date') input = el('input', { type: 'date' });
  else if (type === 'dateTime') input = el('input', { type: 'datetime-local', step: '1' });
  else input = el('input', { spellcheck: 'false', placeholder: type === 'iri' ? 'absolute IRI' : 'value' });
  const row = el('label', { class: 'field' },
    el('span', {}, p.name + ' ', el('span', { class: 'chip type', text: type })), input);
  const get = () => { let v = input.value; if (type === 'dateTime' && v && v.length === 16) v += ':00'; return { name: p.name, value: v }; };
  return { row, get };
}

// ------------------------------------------------------------------ graph/base
// The pod mints URIs with its configured base_uri, which may differ from the URL
// we reach it at (a reverse proxy, or base_uri set to a different host/port). So:
//  - extract container members by predicate, never by assuming the subject IRI;
//  - identify a record's primary subject by its rdf:type;
//  - send every HTTP request to the client's Base URL using the path/id, while
//    keeping the pod's own logical URIs where the pod matches them (a grant's
//    linkedView, a blob proxy's uri= param).
let detectedPodBase = null;
const pathId = (u) => (u || '').replace(/[?#].*$/, '').replace(/\/+$/, '').split('/').pop();
const containsMembers = (tr) => [...new Set(tr.filter((t) => t.p.value === RDF.T.contains).map((t) => t.o.value))].sort();
function firstContainerSubject(tr) {
  const typed = tr.find((t) => t.p.value === RDF.T.type && (t.o.value === RDF.NS.ldp + 'BasicContainer' || t.o.value === RDF.NS.ldp + 'Container'));
  if (typed) return typed.s.value;
  const c = tr.find((t) => t.p.value === RDF.T.contains);
  return c ? c.s.value : null;
}
// Learn the pod's base from a container subject (subject === base + requestedPath).
function rememberBase(subject, requestedPath) {
  if (subject != null && requestedPath !== undefined && subject.endsWith(requestedPath)) {
    detectedPodBase = subject.slice(0, subject.length - requestedPath.length);
  }
}
const podBase = () => detectedPodBase || (Pod.origin() + '/');
// A pod-logical URI → a path relative to the pod base (for navigation/addressing).
function toLocalPath(logicalUri) {
  if (detectedPodBase && logicalUri.startsWith(detectedPodBase)) return logicalUri.slice(detectedPodBase.length);
  try { return new URL(logicalUri).pathname.replace(/^\/+/, ''); } catch (e) { return logicalUri; }
}
// A full pod-logical URL → a URL at the client's Base URL, path + query preserved
// (the query — e.g. a blob's uri= and view params — must survive intact).
function localizeUrl(u) {
  if (detectedPodBase && u.startsWith(detectedPodBase)) return Pod.origin() + '/' + u.slice(detectedPodBase.length);
  try { const x = new URL(u); return Pod.origin() + x.pathname + x.search; } catch (e) { return u; }
}

// ============================================================ OWNER · DATA
const parentPath = (p) => { p = p.replace(/\/+$/, ''); const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i + 1); };

async function browse(path) {
  path = (path || '').replace(/^\/+/, '');
  $('#dataPath').value = path;
  const target = $('#browseOut');
  clear(target).appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.url(path), headers: { Accept: 'text/turtle' } });
  clear(target);
  target.appendChild(statusHead(res));
  if (res.network || !res.ok) { target.appendChild(notice(errorDetail(res), 'err')); return; }
  if (res.etag) { $('#writeIfMatch').value = res.etag; $('#writePath').value = path; }

  if (res.blob) {
    target.appendChild(el('div', { class: 'breadcrumb', text: Pod.url(path) }));
    target.appendChild(binaryView(res, filenameForUrl(Pod.url(path), res.contentType))); return;
  }

  const tr = RDF.parseTurtle(res.text);
  const subject = firstContainerSubject(tr);
  rememberBase(subject, path);
  target.appendChild(el('div', { class: 'breadcrumb', text: subject || Pod.url(path) }));
  const members = containsMembers(tr);
  if (members.length) {
    const box = el('div', {});
    box.appendChild(el('div', { class: 'kv', text: members.length + ' member(s):' }));
    for (const m of members) {
      const rel = toLocalPath(m);
      box.appendChild(el('div', { class: 'member-row' },
        el('button', { class: 'link-btn name', onclick: () => browse(rel) }, rel),
        el('button', { class: 'btn ghost small', onclick: () => deleteData(rel) }, 'Delete')));
    }
    target.appendChild(box);
  }
  target.appendChild(el('div', { class: 'row', style: 'margin:.5rem 0' },
    el('button', { class: 'btn ghost small', onclick: () => { $('#writePath').value = path; $('#writeMethod').value = path.endsWith('/') || path === '' ? 'POST' : 'PUT'; syncWriteForm(); showTab('data'); flash($('#writePath')); } }, 'Edit here'),
    path ? el('button', { class: 'btn ghost small', onclick: () => deleteData(path) }, 'Delete this resource') : null));
  target.appendChild(codeBlock(res.text, 'Turtle'));
}

async function deleteData(localPath) {
  const url = Pod.url(localPath);
  if (!confirm('DELETE ' + url + ' ?')) return;
  const res = await Pod.asAdmin({ method: 'DELETE', url });
  toast(res.ok ? 'Deleted' : ('Delete ' + res.status + ' — ' + errorDetail(res)), res.ok ? 'ok' : 'err');
  if (res.ok) browse(parentPath($('#dataPath').value));
}

function syncWriteForm() {
  const binary = $('#writeCtype').value === '__binary';
  $('#rdfBodyWrap').hidden = binary;
  $('#binBodyWrap').hidden = !binary;
  $('#slugField').style.opacity = $('#writeMethod').value === 'POST' ? '1' : '.4';
}

async function writeResource() {
  const path = $('#writePath').value.trim().replace(/^\/+/, '');
  const method = $('#writeMethod').value;
  const sel = $('#writeCtype').value;
  const headers = {};
  let body, ct;
  if (sel === '__binary') {
    const f = $('#writeFile').files[0];
    if (!f) { toast('Choose a file to upload', 'err'); return; }
    body = f; ct = $('#writeBinCtype').value.trim() || f.type || 'application/octet-stream';
  } else { body = $('#writeBody').value; ct = sel; }
  headers['Content-Type'] = ct;
  if (method === 'POST') { const slug = $('#writeSlug').value.trim(); if (slug) headers['Slug'] = slug; }
  else {
    if ($('#writeCreateOnly').checked) headers['If-None-Match'] = '*';
    else if ($('#writeUseEtag').checked && $('#writeIfMatch').value.trim()) headers['If-Match'] = $('#writeIfMatch').value.trim();
  }
  const res = await Pod.asAdmin({ method, url: Pod.url(path), headers, body });
  renderResponse($('#writeOut'), res, { successNote: method + ' → ' + res.status + (res.location ? ' · ' + res.location : '') });
  if (res.ok && res.etag) $('#writeIfMatch').value = res.etag;
}

// ============================================================ OWNER · VIEWS
async function createView() {
  const title = $('#viewTitle').value.trim();
  if (!title) { toast('Title is required', 'err'); return; }
  const ttl = RDF.viewTurtle({
    title, description: $('#viewDesc').value.trim(), template: $('#viewTemplate').value,
    contentType: $('#viewCtype').value, maxRetrievals: $('#viewMax').value.trim(),
    params: collectParams($('#viewParams')),
  });
  const slug = $('#viewSlug').value.trim();
  const headers = { 'Content-Type': 'text/turtle' };
  let res;
  if ($('#viewReplace').checked && slug) res = await Pod.asAdmin({ method: 'PUT', url: Pod.ns.view(slug), headers, body: ttl });
  else { if (slug) headers['Slug'] = slug; res = await Pod.asAdmin({ method: 'POST', url: Pod.origin() + '/.system/views', headers, body: ttl }); }
  renderResponse($('#viewOut'), res, { successNote: res.ok ? ('View saved' + (res.location ? ' → ' + res.location : '')) : undefined });
  if (res.ok) loadViews();
}

async function loadViews() {
  const target = clear($('#viewsList'));
  target.appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.viewsContainer(), headers: { Accept: 'text/turtle' } });
  clear(target);
  if (!res.ok) { target.appendChild(notice(errorDetail(res), 'err')); return; }
  const tr = RDF.parseTurtle(res.text);
  rememberBase(firstContainerSubject(tr), '.system/views/');
  const members = containsMembers(tr);
  if (!members.length) { target.appendChild(el('p', { class: 'muted small', text: 'No views yet.' })); return; }
  for (const uri of members) target.appendChild(await viewCard(uri));
}

async function viewCard(uri) {
  const id = pathId(uri);
  const item = el('div', { class: 'item' });
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.view(id), headers: { Accept: 'text/turtle' } });
  let title = id, desc = '', params = [];
  if (res.ok) {
    const tr = RDF.parseTurtle(res.text);
    const subj = RDF.subjectsOfType(tr, RDF.T.View)[0] || uri;
    title = RDF.value(tr, subj, RDF.T.title) || id;
    desc = RDF.value(tr, subj, RDF.T.description) || '';
    params = RDF.objects(tr, subj, RDF.T.parameter).map((o) => ({
      name: RDF.value(tr, o.value, RDF.T.paramName), type: RDF.value(tr, o.value, RDF.T.paramType),
    }));
  }
  item.appendChild(el('div', { class: 'item-head' },
    el('span', { class: 'item-title', text: title }), el('span', { class: 'item-id', text: id })));
  if (desc) item.appendChild(el('div', { class: 'item-desc', text: desc }));
  if (params.length) item.appendChild(el('div', {}, ...params.map((p) => el('span', { class: 'chip' }, p.name + ' : ' + p.type))));
  const body = el('div', { class: 'item-body' });
  item.appendChild(el('div', { class: 'item-actions' },
    el('button', { class: 'btn ghost small', onclick: () => inspectView(id, body) }, 'Definition'),
    el('button', { class: 'btn ghost small', onclick: () => addViewToGrant(uri) }, 'Add to grant'),
    el('button', { class: 'btn ghost small', onclick: () => deleteView(id) }, 'Delete')));
  item.appendChild(body);
  return item;
}
async function inspectView(id, body) {
  clear(body).appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.view(id), headers: { Accept: 'text/turtle' } });
  clear(body);
  if (!res.ok) {
    body.appendChild(statusHead(res));
    body.appendChild(notice(errorDetail(res), 'err'));
    if (res.text) body.appendChild(codeBlock(res.text, 'response body'));
    return;
  }
  body.appendChild(codeBlock(res.text, 'Turtle definition'));
}
async function deleteView(id) {
  if (!confirm('Delete view ' + id + '?')) return;
  const res = await Pod.asAdmin({ method: 'DELETE', url: Pod.ns.view(id) });
  toast(res.ok ? 'View deleted' : ('Delete ' + res.status), res.ok ? 'ok' : 'err');
  if (res.ok) loadViews();
}

// ============================================================ OWNER · GRANTS
function ensureViewCheckbox(uri, checked = true) {
  const box = $('#grantViews');
  let cb = $$('input[type=checkbox]', box).find((i) => i.value === uri);
  if (!cb) {
    if (box.querySelector('.muted')) clear(box);
    const label = el('label', {},
      el('input', { type: 'checkbox', value: uri }), el('span', {}, pathId(uri)), el('span', { class: 'vid', text: uri }));
    box.appendChild(label); cb = label.querySelector('input');
  }
  cb.checked = checked;
  return cb;
}
function addViewToGrant(uri) { showTab('grants', 'owner'); flash(ensureViewCheckbox(uri, true).closest('label')); toast('Added to grant', 'ok'); }

async function loadGrantViews() {
  const box = clear($('#grantViews'));
  box.appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.viewsContainer(), headers: { Accept: 'text/turtle' } });
  clear(box);
  if (!res.ok) { box.appendChild(notice(errorDetail(res), 'err')); return; }
  const tr = RDF.parseTurtle(res.text);
  rememberBase(firstContainerSubject(tr), '.system/views/');
  const members = containsMembers(tr);
  if (!members.length) { box.appendChild(el('p', { class: 'muted small', text: 'No views to link — create one first.' })); return; }
  // Checkbox values are the pod's own logical view URIs — a grant's linkedView must
  // be what the pod stores and matches at fetch time, not the client's Base URL.
  for (const m of members) box.appendChild(el('label', {},
    el('input', { type: 'checkbox', value: m }), el('span', {}, pathId(m)), el('span', { class: 'vid', text: m })));
}

async function issueGrant() {
  const viewUris = $$('#grantViews input:checked').map((i) => i.value);
  const ttl = RDF.grantTurtle({ title: $('#grantTitle').value.trim(), viewUris });
  const res = await Pod.asAdmin({ method: 'POST', url: Pod.origin() + '/.system/tokens', headers: { 'Content-Type': 'text/turtle' }, body: ttl });
  renderResponse($('#grantOut'), res);
  if (res.ok && res.text) {
    showSecret(RDF.extractLiteral(res.text, 'tokenSecret'), res.location, RDF.extractIri(res.text, 'policyRef'));
    loadGrants();
  }
}
function showSecret(secret, location, policyRef) {
  const box = $('#grantSecretBox'); box.hidden = false; clear(box);
  box.appendChild(notice('Grant issued — copy the secret now. It is shown only once and cannot be retrieved again.', 'ok'));
  box.appendChild(el('div', { class: 'kv', text: 'Consumer bearer token (pod:tokenSecret)' }));
  box.appendChild(el('div', { class: 'secret-val', text: secret || '(secret not found in response)' }));
  box.appendChild(el('div', { class: 'row' },
    el('button', { class: 'btn small', onclick: () => copy(secret) }, 'Copy secret'),
    el('button', { class: 'btn primary small', onclick: () => { Pod.setConsumer(secret); $('#consumerToken').value = secret; toast('Saved as consumer token', 'ok'); } }, 'Use as consumer token')));
  if (location) box.appendChild(el('div', { class: 'kv', text: 'Grant record: ' + location }));
  if (policyRef) {
    const pid = pathId(policyRef);
    box.appendChild(el('div', { class: 'row' },
      el('span', { class: 'kv', text: 'policy id: ' + pid }),
      el('button', { class: 'btn ghost small', onclick: () => gotoPolicy(pid) }, 'Bound this grant with a policy')));
  }
}

async function loadGrants() {
  const target = clear($('#grantsList'));
  target.appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.tokensContainer(), headers: { Accept: 'text/turtle' } });
  clear(target);
  if (!res.ok) { target.appendChild(notice(errorDetail(res), 'err')); return; }
  const tr = RDF.parseTurtle(res.text);
  rememberBase(firstContainerSubject(tr), '.system/tokens/');
  const members = containsMembers(tr).filter((m) => !m.endsWith('/policies/'));
  if (!members.length) { target.appendChild(el('p', { class: 'muted small', text: 'No grants yet.' })); return; }
  for (const uri of members) target.appendChild(await grantCard(uri));
}
async function grantCard(uri) {
  const id = pathId(uri);
  const item = el('div', { class: 'item' });
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.token(id), headers: { Accept: 'text/turtle' } });
  let kind = 'Token', linked = [], policyRef = null, count = '0';
  if (res.ok) {
    const tr = RDF.parseTurtle(res.text);
    const subj = RDF.subjectsOfType(tr, RDF.T.Token)[0] || uri;
    const types = RDF.values(tr, subj, RDF.T.type);
    kind = types.includes(RDF.NS.pod + 'ConsumerToken') ? 'ConsumerToken'
      : types.includes(RDF.NS.pod + 'AdminToken') ? 'AdminToken'
      : types.includes(RDF.NS.pod + 'EngineToken') ? 'EngineToken' : 'Token';
    linked = RDF.values(tr, subj, RDF.T.linkedView);
    policyRef = RDF.value(tr, subj, RDF.T.policyRef);
    count = RDF.value(tr, subj, RDF.T.enforcementCount) || '0';
  }
  item.appendChild(el('div', { class: 'item-head' },
    el('span', { class: 'item-title', text: id }),
    el('span', { class: kind === 'ConsumerToken' ? 'chip' : 'chip type', text: kind }),
    el('span', { class: 'item-id', text: 'deliveries: ' + count })));
  if (linked.length) item.appendChild(el('div', {}, ...linked.map((v) => el('span', { class: 'chip', text: pathId(v) }))));
  else item.appendChild(el('div', { class: 'item-desc', text: kind === 'ConsumerToken' ? 'unlocks no views' : '(unscoped credential)' }));
  const actions = el('div', { class: 'item-actions' });
  if (policyRef) actions.appendChild(el('button', { class: 'btn ghost small', onclick: () => gotoPolicy(pathId(policyRef)) }, 'Edit policy'));
  if (kind === 'ConsumerToken') actions.appendChild(el('button', { class: 'btn ghost small', onclick: () => revokeGrant(id) }, 'Revoke'));
  else actions.appendChild(el('span', { class: 'kv', text: 'revoking this would break the pod' }));
  item.appendChild(actions);
  return item;
}
async function revokeGrant(id) {
  if (!confirm('Revoke grant ' + id + '? The token dies immediately.')) return;
  const res = await Pod.asAdmin({ method: 'DELETE', url: Pod.ns.token(id) });
  toast(res.ok ? 'Grant revoked' : ('Revoke ' + res.status), res.ok ? 'ok' : 'err');
  if (res.ok) loadGrants();
}

// ============================================================ OWNER · POLICIES
const pad = (n) => String(n).padStart(2, '0');
function localToXsdUtc(v) {
  if (!v) return '';
  const d = new Date(v); if (isNaN(d)) return '';
  return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + 'T' +
    pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + 'Z';
}
function xsdToLocalInput(x) {
  if (!x) return '';
  const d = new Date(x); if (isNaN(d)) return '';
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' +
    pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}
function policyConstraints() {
  return [
    { pred: 'expiresAt', value: localToXsdUtc($('#polExpiresAt').value), xsd: 'dateTime' },
    { pred: 'validFrom', value: localToXsdUtc($('#polValidFrom').value), xsd: 'dateTime' },
    { pred: 'validUntil', value: localToXsdUtc($('#polValidUntil').value), xsd: 'dateTime' },
    { pred: 'maxRetrievals', value: $('#polMaxRetrievals').value.trim(), xsd: 'integer' },
    { pred: 'minInterval', value: $('#polMinInterval').value.trim(), xsd: 'integer' },
  ];
}
function gotoPolicy(pid) { showTab('policies', 'owner'); $('#policyId').value = pid; flash($('#policyId')); }
async function savePolicy() {
  const id = $('#policyId').value.trim();
  if (!id) { toast('Enter a policy id', 'err'); return; }
  const ttl = RDF.policyTurtle(policyConstraints());
  const res = await Pod.asAdmin({ method: 'PUT', url: Pod.ns.policy(id), headers: { 'Content-Type': 'text/turtle' }, body: ttl });
  renderResponse($('#policyOut'), res, { successNote: res.ok ? 'Policy saved' : undefined });
}
async function loadPolicy() {
  const id = $('#policyId').value.trim();
  if (!id) { toast('Enter a policy id', 'err'); return; }
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.policy(id), headers: { Accept: 'text/turtle' } });
  renderResponse($('#policyOut'), res);
  if (!res.ok) return;
  const tr = RDF.parseTurtle(res.text);
  const u = RDF.subjectsOfType(tr, RDF.T.Policy)[0] || Pod.ns.policy(id);
  $('#polExpiresAt').value = xsdToLocalInput(RDF.value(tr, u, RDF.NS.pod + 'expiresAt'));
  $('#polValidFrom').value = xsdToLocalInput(RDF.value(tr, u, RDF.NS.pod + 'validFrom'));
  $('#polValidUntil').value = xsdToLocalInput(RDF.value(tr, u, RDF.NS.pod + 'validUntil'));
  $('#polMaxRetrievals').value = RDF.value(tr, u, RDF.NS.pod + 'maxRetrievals') || '';
  $('#polMinInterval').value = RDF.value(tr, u, RDF.NS.pod + 'minInterval') || '';
}
function clearPolicyFields() {
  ['polExpiresAt', 'polValidFrom', 'polValidUntil', 'polMaxRetrievals', 'polMinInterval'].forEach((i) => { $('#' + i).value = ''; });
  $('#policyPreview').hidden = true;
}

// ============================================================ OWNER · SPARQL
async function runSparql() {
  const q = $('#sparqlQuery').value;
  const method = $('#sparqlMethod').value;
  const qs = new URLSearchParams();
  if ($('#sparqlSystem').checked) qs.set('include-system', 'true');
  for (const r of $$('#sparqlBindings .param-row')) {
    const name = r.querySelector('.k').value.trim();
    if (!name) continue;
    qs.set('binding-' + name, r.querySelector('.v').value);
    const dt = r.querySelector('.dt').value.trim();
    if (dt) qs.set('bindingtype-' + name, dt);
  }
  let url = Pod.origin() + '/sparql', headers = { Accept: $('#sparqlAccept').value }, body = null;
  if (method === 'GET') { qs.set('query', q); url += '?' + qs.toString(); }
  else { headers['Content-Type'] = 'application/sparql-query'; body = q; const s = qs.toString(); if (s) url += '?' + s; }
  const res = await Pod.asAdmin({ method, url, headers, body });
  renderSparql($('#sparqlOut'), res);
}
function renderSparql(target, res) {
  clear(target); target.appendChild(statusHead(res));
  if (res.network || !res.ok) { target.appendChild(notice(errorDetail(res), 'err')); if (res.text) target.appendChild(codeBlock(res.text)); return; }
  if (/sparql-results\+json/.test(res.contentType || '')) {
    try { target.appendChild(renderResultsJson(JSON.parse(res.text))); return; } catch (e) { /* fall through */ }
  }
  target.appendChild(codeBlock(res.text != null ? res.text : '(no body)'));
}
function renderResultsJson(j) {
  if (typeof j.boolean === 'boolean') return notice('ASK → ' + j.boolean, j.boolean ? 'ok' : 'info');
  const vars = j.head?.vars || [];
  const rows = j.results?.bindings || [];
  const wrap = el('div', {}, el('div', { class: 'kv', text: rows.length + ' row(s)' }));
  const table = el('table', { class: 'data' });
  table.appendChild(el('tr', {}, ...vars.map((v) => el('th', { text: v }))));
  for (const b of rows) table.appendChild(el('tr', {}, ...vars.map((v) => el('td', { text: b[v] ? b[v].value : '' }))));
  wrap.appendChild(table);
  return wrap;
}

// ============================================================ OWNER · STATS
async function loadStats() {
  const target = clear($('#statsOut'));
  const res = await Pod.asAdmin({ method: 'GET', url: Pod.ns.stats(), headers: { Accept: 'application/json' } });
  target.appendChild(statusHead(res));
  if (!res.ok) { target.appendChild(notice(errorDetail(res), 'err')); return; }
  let j; try { j = JSON.parse(res.text); } catch (e) { target.appendChild(codeBlock(res.text)); return; }
  target.appendChild(notice('Total deliveries: ' + j.total, 'info'));
  if (j.by_view?.length) {
    target.appendChild(el('div', { class: 'kv', text: 'By view' }));
    const t = el('table', { class: 'data' }, el('tr', {}, el('th', { text: 'view' }), el('th', { text: 'count' }), el('th', { text: 'last delivery' })));
    for (const v of j.by_view) t.appendChild(el('tr', {}, el('td', { text: v.view_uri.split('/').pop() }), el('td', { text: String(v.count) }), el('td', { text: v.last_accessed_at })));
    target.appendChild(t);
  }
  if (j.by_token?.length) {
    target.appendChild(el('div', { class: 'kv', text: 'By grant' }));
    const t = el('table', { class: 'data' }, el('tr', {}, el('th', { text: 'grant' }), el('th', { text: 'count' })));
    for (const v of j.by_token) t.appendChild(el('tr', {}, el('td', { text: v.token_uri.split('/').pop() }), el('td', { text: String(v.count) })));
    target.appendChild(t);
  }
}

// ============================================================ CONSUMER
async function discover() {
  const target = clear($('#discoverList'));
  target.appendChild(el('p', { class: 'muted small', text: 'Loading…' }));
  const res = await Pod.asConsumer({ method: 'GET', url: Pod.ns.discovery(), headers: { Accept: 'text/turtle' } });
  clear(target);
  if (!res.ok) { target.appendChild(statusHead(res)); target.appendChild(notice(errorDetail(res), 'err')); return; }
  const tr = RDF.parseTurtle(res.text);
  rememberBase(firstContainerSubject(tr), '.engine/discovery');
  const members = containsMembers(tr);
  if (!members.length) { target.appendChild(el('p', { class: 'muted small', text: 'This grant unlocks no views.' })); return; }
  for (const m of members) target.appendChild(discoverCard(m, tr));
}
function discoverCard(memberUri, tr) {
  const id = pathId(memberUri);
  const title = RDF.value(tr, memberUri, RDF.T.title) || id;
  const desc = RDF.value(tr, memberUri, RDF.T.description) || '';
  const params = RDF.objects(tr, memberUri, RDF.T.parameter).map((o) => ({
    name: RDF.value(tr, o.value, RDF.T.paramName), type: RDF.value(tr, o.value, RDF.T.paramType),
  }));
  const item = el('div', { class: 'item' });
  item.appendChild(el('div', { class: 'item-head' },
    el('span', { class: 'item-title', text: title }), el('span', { class: 'item-id', text: id })));
  if (desc) item.appendChild(el('div', { class: 'item-desc', text: desc }));
  const form = el('div', { class: 'params' });
  const getters = [];
  if (params.length) { item.appendChild(el('div', { class: 'params-head' }, el('span', {}, 'Parameters'))); for (const p of params) { const { row, get } = typedParamInput(p); form.appendChild(row); getters.push(get); } }
  item.appendChild(form);
  const out = el('div', { class: 'item-body' });
  item.appendChild(el('div', { class: 'item-actions' },
    el('button', { class: 'btn primary small', onclick: () => fetchView(id, getters.map((g) => g()), out) }, 'Fetch result')));
  item.appendChild(out);
  return item;
}
async function fetchView(id, params, target) {
  const qs = new URLSearchParams();
  for (const p of params) if (p.value !== '' && p.value != null) qs.set(p.name, p.value);
  const url = Pod.ns.engineView(id) + (qs.toString() ? '?' + qs.toString() : '');
  const res = await Pod.asConsumer({ method: 'GET', url });
  renderViewResult(target, res);
}
function renderViewResult(target, res) {
  clear(target); target.appendChild(statusHead(res));
  if (res.network || !res.ok) { target.appendChild(notice(errorDetail(res), 'err')); if (res.text) target.appendChild(codeBlock(res.text)); return; }
  if (res.blob) { target.appendChild(binaryView(res)); return; }
  target.appendChild(codeBlock(res.text, res.contentType));
  const blobs = detectBlobUrls(res.text);
  if (blobs.length) {
    const box = el('div', {}, el('div', { class: 'kv', text: 'Shared resources (dereference with this grant):' }));
    for (const b of blobs) box.appendChild(el('div', { class: 'member-row' },
      el('span', { class: 'name', text: decodeURIComponent(b) }),
      el('button', { class: 'btn small', onclick: () => consumerDownload(b) }, 'Download')));
    target.appendChild(box);
  }
}
const detectBlobUrls = (text) => [...new Set(text.match(/https?:\/\/[^\s"'<>\\]+\/\.engine\/blob\/[^\s"'<>\\]*/g) || [])];

async function consumerDownload(url) {
  // The proxy URL uses the pod's own base; send it to our Base URL, but keep the
  // query (its uri= names a pod-base data resource the pod re-validates as-is).
  const res = await Pod.asConsumer({ method: 'GET', url: localizeUrl(url) });
  if (res.ok && res.blob) { triggerDownload(res.blob, filenameForUrl(url, res.contentType)); toast('Downloaded', 'ok'); }
  else if (res.ok) { toast('Fetched (not binary)', 'ok'); }
  else toast('Blob ' + res.status + ' — ' + errorDetail(res), 'err');
}
async function manualFetch() {
  const id = $('#cViewId').value.trim();
  if (!id) { toast('Enter a view id', 'err'); return; }
  const params = $$('#cParams .param-row').map((r) => ({ name: r.querySelector('.k').value.trim(), value: r.querySelector('.v').value })).filter((p) => p.name);
  fetchView(id, params, $('#cFetchOut'));
}
async function manualBlob() {
  const url = $('#cBlobUrl').value.trim();
  if (!url) { toast('Paste a blob URL', 'err'); return; }
  const res = await Pod.asConsumer({ method: 'GET', url: localizeUrl(url) });
  const target = clear($('#cBlobOut'));
  target.appendChild(statusHead(res));
  if (!res.ok) { target.appendChild(notice(errorDetail(res), 'err')); return; }
  if (res.blob) { const name = filenameForUrl(url, res.contentType); triggerDownload(res.blob, name); target.appendChild(binaryView(res, name)); }
  else target.appendChild(codeBlock(res.text));
}

// ============================================================ LOG DRAWER
function renderLog(log) {
  $('#logCount').textContent = log.length;
  const body = clear($('#logBody'));
  if (!log.length) { body.appendChild(el('p', { class: 'muted small', text: 'No requests yet.' })); return; }
  for (const e of log) {
    const det = el('details', { class: 'log-entry' });
    det.appendChild(el('summary', {},
      el('span', { class: 'method status-' + String(e.status)[0], text: e.method }),
      el('span', { class: 'url', text: e.url }),
      el('span', { class: 'kv', text: (e.status || 'ERR') + ' · ' + e.ms + 'ms' })));
    const d = el('div', { class: 'detail' });
    if (e.error) d.appendChild(notice(e.error, 'err'));
    d.appendChild(el('pre', { class: 'code', text: '▸ request\n' + (e.reqHeaders || []).map(([k, v]) => k + ': ' + v).join('\n') + (e.reqBody ? '\n\n' + e.reqBody : '') }));
    d.appendChild(el('pre', { class: 'code', text: '▸ response\n' + (e.respHeaders || []).map(([k, v]) => k + ': ' + v).join('\n') + '\n\n' + (e.respPreview || '') }));
    det.appendChild(d);
    body.appendChild(det);
  }
}

// ============================================================ INIT
function bindPersistentInput(id, key, fallback) {
  const input = $('#' + id);
  input.value = localStorage.getItem(key) ?? fallback;
  input.addEventListener('input', () => localStorage.setItem(key, input.value));
  return input;
}

function init() {
  // connection + credentials
  bindPersistentInput('baseUrl', Pod.KEYS.base, Pod.DEFAULT_BASE)
    .addEventListener('change', checkHealth);
  bindPersistentInput('adminToken', Pod.KEYS.admin, '');
  bindPersistentInput('consumerToken', Pod.KEYS.consumer, '');

  $$('[data-clear]').forEach((b) => b.addEventListener('click', () => {
    const input = $('#' + b.dataset.clear); input.value = ''; localStorage.setItem('ldpc.' + (b.dataset.clear === 'adminToken' ? 'admin' : 'consumer'), ''); toast('Cleared', 'ok');
  }));
  $$('[data-reveal]').forEach((b) => b.addEventListener('click', () => {
    const input = $('#' + b.dataset.reveal); input.type = input.type === 'password' ? 'text' : 'password';
  }));

  // role + tabs
  $$('[data-role]').forEach((b) => b.addEventListener('click', () => applyRole(b.dataset.role)));
  $$('.tab').forEach((b) => b.addEventListener('click', () => activateTab(b.dataset.tab)));

  // health + log
  $('#btnHealth').addEventListener('click', checkHealth);
  $('#btnLog').addEventListener('click', () => { const d = $('#logDrawer'); d.hidden = !d.hidden; });
  $('#btnLogClose').addEventListener('click', () => { $('#logDrawer').hidden = true; });
  $('#btnLogClear').addEventListener('click', Pod.clearLog);
  Pod.onLog(renderLog);

  // data
  $('#btnBrowse').addEventListener('click', () => browse($('#dataPath').value));
  $('#btnBrowseUp').addEventListener('click', () => browse(parentPath($('#dataPath').value)));
  $('#dataPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') browse($('#dataPath').value); });
  $('#writeCtype').addEventListener('change', syncWriteForm);
  $('#writeMethod').addEventListener('change', syncWriteForm);
  $('#btnWrite').addEventListener('click', writeResource);
  syncWriteForm();

  // views
  $('#btnAddViewParam').addEventListener('click', () => paramRow($('#viewParams')));
  $('#btnViewPreview').addEventListener('click', () => {
    const pre = $('#viewPreview'); pre.hidden = false;
    pre.textContent = RDF.viewTurtle({
      title: $('#viewTitle').value.trim() || 'Untitled', description: $('#viewDesc').value.trim(),
      template: $('#viewTemplate').value, contentType: $('#viewCtype').value,
      maxRetrievals: $('#viewMax').value.trim(), params: collectParams($('#viewParams')),
    });
  });
  $('#btnViewCreate').addEventListener('click', createView);
  $('#btnViewsRefresh').addEventListener('click', loadViews);

  // grants
  $('#btnGrantRefresh').addEventListener('click', loadGrantViews);
  $('#btnGrantsRefresh').addEventListener('click', loadGrants);
  $('#btnGrantIssue').addEventListener('click', issueGrant);
  $('#btnGrantAddCustom').addEventListener('click', () => {
    const v = $('#grantCustomView').value.trim(); if (!v) return;
    ensureViewCheckbox(/^https?:\/\//.test(v) ? v : podBase() + '.system/views/' + v, true);
    $('#grantCustomView').value = '';
  });

  // policies
  $('#btnPolicyLoad').addEventListener('click', loadPolicy);
  $('#btnPolicySave').addEventListener('click', savePolicy);
  $('#btnPolicyClear').addEventListener('click', clearPolicyFields);
  $('#btnPolicyPreview').addEventListener('click', () => { const p = $('#policyPreview'); p.hidden = false; p.textContent = RDF.policyTurtle(policyConstraints()); });

  // sparql
  $('#btnAddBinding').addEventListener('click', () => {
    const row = kvRow($('#sparqlBindings'));
    row.insertBefore(el('input', { class: 'dt', spellcheck: 'false', placeholder: 'xsd type (optional)' }), row.lastChild);
  });
  $('#btnSparqlRun').addEventListener('click', runSparql);

  // stats
  $('#btnStats').addEventListener('click', loadStats);

  // consumer
  $('#btnDiscover').addEventListener('click', discover);
  $('#btnAddCParam').addEventListener('click', () => kvRow($('#cParams')));
  $('#btnCFetch').addEventListener('click', manualFetch);
  $('#btnCBlob').addEventListener('click', manualBlob);

  applyRole(Pod.role());
  checkHealth();
}

document.addEventListener('DOMContentLoaded', init);
