/* rdf.js — zero-dependency RDF helpers for the Test Console.
 *
 * Reads use N-Triples (Accept: application/n-triples): a flat, line-oriented
 * syntax that is trivial and robust to parse without a library. Writes build
 * small, well-formed Turtle documents with correct literal escaping. The one
 * exception is the token-issuance response, which the server always returns as
 * Turtle; its one-time secret is lifted out with a targeted extractor. */

'use strict';

const RDF = (function () {
  const NS = {
    pod: 'https://lukasberka.github.io/ldp-personal-store/vocab#',
    dcterms: 'http://purl.org/dc/terms/',
    ldp: 'http://www.w3.org/ns/ldp#',
    rdf: 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    xsd: 'http://www.w3.org/2001/XMLSchema#',
  };

  // Fully-qualified predicate/class IRIs used throughout the client.
  const T = {
    type: NS.rdf + 'type',
    contains: NS.ldp + 'contains',
    title: NS.dcterms + 'title',
    description: NS.dcterms + 'description',
    constructTemplate: NS.pod + 'constructTemplate',
    contentTypeHint: NS.pod + 'contentTypeHint',
    parameter: NS.pod + 'parameter',
    paramName: NS.pod + 'paramName',
    paramType: NS.pod + 'paramType',
    maxViewRetrievals: NS.pod + 'maxViewRetrievals',
    viewRetrievalCount: NS.pod + 'viewRetrievalCount',
    linkedView: NS.pod + 'linkedView',
    policyRef: NS.pod + 'policyRef',
    enforcementCount: NS.pod + 'enforcementCount',
    lastUsedAt: NS.pod + 'lastUsedAt',
    tokenSecret: NS.pod + 'tokenSecret',
    View: NS.pod + 'View',
    Policy: NS.pod + 'Policy',
    Token: NS.pod + 'Token',
    ConsumerToken: NS.pod + 'ConsumerToken',
  };

  // ------------------------------------------------------------------ N-Triples

  function parseNTriples(text) {
    const out = [];
    if (!text) return out;
    const n = text.length;
    let i = 0;
    const isWs = (c) => c === ' ' || c === '\t' || c === '\r' || c === '\n';
    const skipWs = () => { while (i < n && isWs(text[i])) i++; };

    function unicode(len) {
      const hex = text.substr(i + 2, len);
      i += 2 + len;
      return String.fromCodePoint(parseInt(hex, 16));
    }
    function readIRI() {
      i++; // '<'
      let s = '';
      while (i < n && text[i] !== '>') {
        if (text[i] === '\\') {
          const e = text[i + 1];
          if (e === 'u') { s += unicode(4); continue; }
          if (e === 'U') { s += unicode(8); continue; }
          s += e; i += 2; continue;
        }
        s += text[i++];
      }
      i++; // '>'
      return s;
    }
    function readBlank() {
      let s = '';
      while (i < n && !isWs(text[i]) && text[i] !== '.') s += text[i++];
      return s;
    }
    function unescape(e) {
      switch (e) {
        case 'n': return '\n'; case 't': return '\t'; case 'r': return '\r';
        case 'b': return '\b'; case 'f': return '\f';
        default: return e; // covers \" \\ \'
      }
    }
    function readLiteral() {
      i++; // opening quote
      let s = '';
      while (i < n) {
        const c = text[i];
        if (c === '\\') {
          const e = text[i + 1];
          if (e === 'u') { s += unicode(4); continue; }
          if (e === 'U') { s += unicode(8); continue; }
          s += unescape(e); i += 2; continue;
        }
        if (c === '"') { i++; break; }
        s += c; i++;
      }
      let datatype = null, lang = null;
      if (text[i] === '^' && text[i + 1] === '^') { i += 2; skipWs(); if (text[i] === '<') datatype = readIRI(); }
      else if (text[i] === '@') { i++; let l = ''; while (i < n && /[A-Za-z0-9-]/.test(text[i])) l += text[i++]; lang = l; }
      return { type: 'literal', value: s, datatype, lang };
    }
    function readTerm() {
      skipWs();
      const c = text[i];
      if (c === '<') return { type: 'iri', value: readIRI() };
      if (c === '_') return { type: 'bnode', value: readBlank() };
      if (c === '"') return readLiteral();
      return null;
    }

    while (i < n) {
      skipWs();
      if (i >= n) break;
      if (text[i] === '#') { while (i < n && text[i] !== '\n') i++; continue; }
      const s = readTerm();
      if (!s) { while (i < n && text[i] !== '\n') i++; continue; }
      const p = readTerm();
      const o = readTerm();
      skipWs();
      if (text[i] === '.') i++;
      if (s && p && o) out.push({ s, p, o });
    }
    return out;
  }

  // ------------------------------------------------------------------ Turtle in
  // A pragmatic Turtle parser producing the same triple shape as parseNTriples.
  // Needed because the reserved-prefix endpoints (/.system/*, /.engine/discovery)
  // always serialize Turtle regardless of Accept. Handles @prefix/@base, `a`,
  // prefixed names, blank-node property lists, collections, and the full range of
  // literals (long strings, datatypes, language tags, numbers, booleans). N-Triples
  // is a strict subset, so this parser reads those responses too.
  const RDF_TYPE = NS.rdf + 'type';

  function parseTurtle(input, baseIRI) {
    const triples = [];
    if (!input) return triples;
    const prefixes = Object.create(null);
    let base = baseIRI || '';
    let i = 0, bnode = 0;
    const n = input.length;
    const err = (m) => new Error('Turtle parse error at ' + i + ': ' + m);
    const isWs = (c) => c === ' ' || c === '\t' || c === '\r' || c === '\n';
    const DELIM = ';,.()[]{}"\'';
    const boundary = (pos) => pos >= n || isWs(input[pos]) || DELIM.indexOf(input[pos]) >= 0;

    function skip() {
      for (;;) {
        while (i < n && isWs(input[i])) i++;
        if (input[i] === '#') { while (i < n && input[i] !== '\n') i++; continue; }
        break;
      }
    }
    function expect(ch) { skip(); if (input[i] !== ch) throw err('expected ' + ch); i++; }
    function unicode(len) { const h = input.substr(i + 2, len); i += 2 + len; return String.fromCodePoint(parseInt(h, 16)); }
    function esc(e) { switch (e) { case 'n': return '\n'; case 't': return '\t'; case 'r': return '\r'; case 'b': return '\b'; case 'f': return '\f'; default: return e; } }

    function resolve(iri) {
      if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(iri)) return iri; // absolute
      if (!base) return iri;
      try { return new URL(iri, base).href; } catch (e) { return iri; }
    }
    function readIRIREF() {
      i++; // '<'
      let s = '';
      while (i < n && input[i] !== '>') {
        if (input[i] === '\\') { const e = input[i + 1]; if (e === 'u') { s += unicode(4); continue; } if (e === 'U') { s += unicode(8); continue; } s += e; i += 2; continue; }
        s += input[i++];
      }
      i++;
      return resolve(s);
    }
    function readBNodeLabel() { let s = ''; while (i < n && !boundary(i)) s += input[i++]; return s; }
    function readPName() {
      let s = '';
      while (i < n && !isWs(input[i]) && DELIM.indexOf(input[i]) < 0) s += input[i++];
      const idx = s.indexOf(':');
      if (idx < 0) throw err('bad name ' + s);
      const pfx = s.slice(0, idx);
      const ns = prefixes[pfx];
      if (ns === undefined) throw err('unknown prefix "' + pfx + '"');
      return ns + s.slice(idx + 1).replace(/\\([_~.!$&'()*+,;=/?#@%-])/g, '$1');
    }
    function readShort(q) {
      i++; let s = '';
      while (i < n) {
        const c = input[i];
        if (c === '\\') { const e = input[i + 1]; if (e === 'u') { s += unicode(4); continue; } if (e === 'U') { s += unicode(8); continue; } s += esc(e); i += 2; continue; }
        if (c === q) { i++; break; }
        s += c; i++;
      }
      return s;
    }
    function readLong(q) {
      i += 3; let s = '';
      while (i < n) {
        if (input[i] === '\\') { const e = input[i + 1]; if (e === 'u') { s += unicode(4); continue; } if (e === 'U') { s += unicode(8); continue; } s += esc(e); i += 2; continue; }
        if (input[i] === q && input[i + 1] === q && input[i + 2] === q) { i += 3; break; }
        s += input[i++];
      }
      return s;
    }
    function readQuoted() {
      const q = input[i];
      const val = (input[i + 1] === q && input[i + 2] === q) ? readLong(q) : readShort(q);
      let datatype = null, lang = null;
      if (input[i] === '^' && input[i + 1] === '^') { i += 2; datatype = input[i] === '<' ? readIRIREF() : readPName(); }
      else if (input[i] === '@') { i++; let l = ''; while (i < n && /[A-Za-z0-9-]/.test(input[i])) l += input[i++]; lang = l; }
      return { type: 'literal', value: val, datatype, lang };
    }
    function readNumeric() {
      const start = i; if (input[i] === '+' || input[i] === '-') i++;
      let dot = false, exp = false;
      while (i < n) {
        const c = input[i];
        if (c >= '0' && c <= '9') { i++; continue; }
        if (c === '.' && !dot && /[0-9]/.test(input[i + 1])) { dot = true; i++; continue; }
        if ((c === 'e' || c === 'E') && !exp) { exp = true; i++; if (input[i] === '+' || input[i] === '-') i++; continue; }
        break;
      }
      const lex = input.slice(start, i);
      const dt = exp ? NS.xsd + 'double' : dot ? NS.xsd + 'decimal' : NS.xsd + 'integer';
      return { type: 'literal', value: lex, datatype: dt, lang: null };
    }
    function readBlankPropertyList() {
      expect('['); const b = { type: 'bnode', value: '_:g' + (bnode++) }; skip();
      if (input[i] === ']') { i++; return b; }
      predObjList(b); skip(); expect(']');
      return b;
    }
    function readCollection() {
      expect('('); const items = [];
      for (;;) { skip(); if (input[i] === ')') { i++; break; } items.push(readObject()); }
      if (!items.length) return { type: 'iri', value: NS.rdf + 'nil' };
      const head = { type: 'bnode', value: '_:c' + (bnode++) }; let cur = head;
      items.forEach((it, idx) => {
        triples.push({ s: cur, p: { value: NS.rdf + 'first' }, o: it });
        const rest = idx === items.length - 1 ? { type: 'iri', value: NS.rdf + 'nil' } : { type: 'bnode', value: '_:c' + (bnode++) };
        triples.push({ s: cur, p: { value: NS.rdf + 'rest' }, o: rest });
        cur = rest;
      });
      return head;
    }
    function readObject() {
      skip();
      const c = input[i];
      if (c === '<') return { type: 'iri', value: readIRIREF() };
      if (c === '_' && input[i + 1] === ':') { i += 2; return { type: 'bnode', value: '_:' + readBNodeLabel() }; }
      if (c === '[') return readBlankPropertyList();
      if (c === '(') return readCollection();
      if (c === '"' || c === "'") return readQuoted();
      if (/[0-9+.\-]/.test(c)) return readNumeric();
      if (input.startsWith('true', i) && boundary(i + 4)) { i += 4; return { type: 'literal', value: 'true', datatype: NS.xsd + 'boolean', lang: null }; }
      if (input.startsWith('false', i) && boundary(i + 5)) { i += 5; return { type: 'literal', value: 'false', datatype: NS.xsd + 'boolean', lang: null }; }
      return { type: 'iri', value: readPName() };
    }
    function readVerb() {
      skip();
      if (input[i] === 'a' && boundary(i + 1)) { i++; return RDF_TYPE; }
      if (input[i] === '<') return readIRIREF();
      return readPName();
    }
    function objList(subj, verb) {
      for (;;) {
        const o = readObject();
        triples.push({ s: subj, p: { value: verb }, o });
        skip();
        if (input[i] === ',') { i++; continue; }
        break;
      }
    }
    function predObjList(subj) {
      for (;;) {
        skip();
        if (input[i] === ']' || input[i] === '.' || i >= n) break;
        const verb = readVerb();
        objList(subj, verb);
        skip();
        if (input[i] === ';') { i++; continue; }
        break;
      }
    }
    function directive() {
      const save = i;
      if (input.startsWith('@prefix', i)) { i += 7; skip(); }
      else if (/^prefix/i.test(input.substr(i, 6))) { i += 6; skip(); }
      else if (input.startsWith('@base', i)) { i += 5; skip(); base = readIRIREF(); skip(); if (input[i] === '.') i++; return; }
      else if (/^base/i.test(input.substr(i, 4))) { i += 4; skip(); base = readIRIREF(); skip(); return; }
      else { i = save; throw err('bad directive'); }
      let pfx = ''; while (i < n && input[i] !== ':') pfx += input[i++]; i++; // ':'
      skip(); prefixes[pfx.trim()] = readIRIREF(); skip();
      if (input[i] === '.') i++;
    }

    skip();
    while (i < n) {
      skip();
      if (i >= n) break;
      const at = i;
      if (input[i] === '@' || /^(prefix|base)\b/i.test(input.substr(i, 7))) { directive(); continue; }
      // subject
      let subj;
      if (input[i] === '[') { subj = readBlankPropertyList(); skip(); if (input[i] === '.') { i++; continue; } }
      else if (input[i] === '<') subj = { type: 'iri', value: readIRIREF() };
      else if (input[i] === '_' && input[i + 1] === ':') { i += 2; subj = { type: 'bnode', value: '_:' + readBNodeLabel() }; }
      else if (input[i] === '(') subj = readCollection();
      else subj = { type: 'iri', value: readPName() };
      predObjList(subj);
      skip();
      if (input[i] === '.') i++;
      if (i === at) { i++; } // safety: never stall
    }
    return triples;
  }

  // Graph helpers over a parsed triple array.
  const objects = (tr, s, p) => tr.filter((t) => t.s.value === s && t.p.value === p).map((t) => t.o);
  const values = (tr, s, p) => objects(tr, s, p).map((o) => o.value);
  const value = (tr, s, p) => { const o = objects(tr, s, p); return o.length ? o[0].value : null; };
  const subjectsOfType = (tr, type) =>
    tr.filter((t) => t.p.value === T.type && t.o.value === type).map((t) => t.s.value);

  // ------------------------------------------------------------------ Turtle out

  const PREFIXES =
    `@prefix pod: <${NS.pod}> .\n` +
    `@prefix dcterms: <${NS.dcterms}> .\n` +
    `@prefix xsd: <${NS.xsd}> .\n\n`;

  // A single-line quoted literal with full escaping.
  function str(s) {
    return '"' + String(s)
      .replace(/\\/g, '\\\\').replace(/"/g, '\\"')
      .replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t') + '"';
  }
  // A triple-quoted long literal (readable for multi-line SPARQL); quotes and
  // backslashes escaped so the content can never close the string early.
  function long(s) {
    return '"""' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"""';
  }
  const iri = (u) => '<' + String(u).replace(/[<>"{}|^`\\]/g, encodeURIComponent).replace(/ /g, '%20') + '>';
  const typed = (v, xsdLocal) => str(v) + '^^xsd:' + xsdLocal;

  // Build the Turtle body for a view definition.
  function viewTurtle({ title, description, template, contentType, maxRetrievals, params }) {
    let body = PREFIXES + '[] a pod:View ;\n';
    const lines = [`    dcterms:title ${str(title)}`];
    if (description) lines.push(`    dcterms:description ${str(description)}`);
    lines.push(`    pod:constructTemplate ${long(template)}`);
    lines.push(`    pod:contentTypeHint ${str(contentType || 'text/turtle')}`);
    if (maxRetrievals !== '' && maxRetrievals != null) lines.push(`    pod:maxViewRetrievals ${typed(maxRetrievals, 'integer')}`);
    for (const p of params) {
      lines.push(`    pod:parameter [ pod:paramName ${str(p.name)} ; pod:paramType ${str(p.type)} ]`);
    }
    return body + lines.join(' ;\n') + ' .\n';
  }

  // Build the Turtle body for issuing a grant.
  function grantTurtle({ title, viewUris }) {
    const lines = [];
    if (title) lines.push(`    dcterms:title ${str(title)}`);
    if (viewUris.length) lines.push('    pod:linkedView ' + viewUris.map(iri).join(' ,\n                   '));
    if (!lines.length) return PREFIXES + '[] a pod:Token .\n';
    return PREFIXES + '[]\n' + lines.join(' ;\n') + ' .\n';
  }

  // Build the Turtle body for a policy (only set constraints are emitted).
  function policyTurtle(constraints) {
    // constraints: [{pred, value, xsd}]
    const lines = ['    a pod:Policy'];
    for (const c of constraints) {
      if (c.value === '' || c.value == null) continue;
      lines.push(`    pod:${c.pred} ${typed(c.value, c.xsd)}`);
    }
    return PREFIXES + '[]\n' + lines.join(' ;\n') + ' .\n';
  }

  // Lift a value out of the (always-Turtle) token issuance response by local name.
  function extractLiteral(turtle, localName) {
    const m = turtle.match(new RegExp('[#:]' + localName + '(?:>)?\\s+"((?:[^"\\\\]|\\\\.)*)"'));
    return m ? m[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\') : null;
  }
  function extractIri(turtle, localName) {
    const m = turtle.match(new RegExp('[#:]' + localName + '(?:>)?\\s+<([^>]+)>'));
    return m ? m[1] : null;
  }

  return {
    NS, T,
    parseNTriples, parseTurtle, objects, values, value, subjectsOfType,
    str, long, iri, typed, PREFIXES,
    viewTurtle, grantTurtle, policyTurtle,
    extractLiteral, extractIri,
  };
})();

window.RDF = RDF;
