/* Acintyo Predictive Distribution Intelligence Platform: browser console.
 *
 * Vanilla ES2020, no dependencies. Layout of this file:
 *   1. constants + state
 *   2. storage, api helper, errors
 *   3. formatters + escaping
 *   4. HTML building blocks (chips, tiles, tables, key/value, tree)
 *   5. charts (inline SVG)
 *   6. UI services (toast, modal, busy buttons)
 *   7. one render function per tab
 *   8. SKU 360 drawer
 *   9. actions (event delegation)
 *  10. header, routing, bootstrap
 */
'use strict';

/* ======================================================= 1. constants + state */
const API_BASE = '/api/v1';
const ACTIONS = ['COMPLIANCE_HOLD', 'LIQUIDATE', 'SOURCE', 'STOCK', 'DISCOUNT', 'SELL', 'DONT_STOCK'];
const ACTION_PRIORITY = Object.fromEntries(ACTIONS.map((a, i) => [a, i]));
const PO_PRIORITY_RANK = { HIGH: 0, MEDIUM: 1, LOW: 2 };
const PO_PRIORITY_TONE = { HIGH: 'p0', MEDIUM: 'p2', LOW: 'p3' };
const OPEN_PO_STATES = ['DRAFT', 'DEFERRED'];
const ACTIVE_SOURCING = ['OPEN', 'HELD', 'PURCHASE_TASK'];

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'inventory', label: 'Inventory' },
  { id: 'bounce', label: 'Bounce' },
  { id: 'purchase', label: 'Purchase' },
  { id: 'margin', label: 'Margin' },
  { id: 'decisions', label: 'Decisions' },
  { id: 'sourcing', label: 'Sourcing' },
  { id: 'ask', label: 'Ask AI' },
  { id: 'admin', label: 'Admin' },
];

const SUGGESTED_QUESTIONS = [
  'Which SKUs should we stock that keep bouncing?',
  'How much working capital is stuck in slow-moving stock?',
  'ఈ రోజు ఏ products ఎక్కువగా bounce అయ్యాయి?',
  'Which purchase orders are high priority for tomorrow?',
  'Where are we leaking margin on fast movers?',
];

const state = {
  meta: null,
  tab: 'overview',
  wh: '',
  date: '',
  key: '',
  renderSeq: 0,
  po: { rows: [], dash: null, lines: {}, edits: {}, selected: new Set(), status: '', priority: '' },
  dec: { action: '', open: new Set(), items: {}, explain: {} },
  sourcing: { status: '', parse: null },
  chat: [],
  chatBusy: false,
  briefing: null,
  llm: null,
  cycle: null,
};

/* ==================================================== 2. storage + api helper */
const store = {
  get(k) { try { return window.localStorage.getItem(k); } catch { return null; } },
  set(k, v) {
    try {
      if (v) window.localStorage.setItem(k, v);
      else window.localStorage.removeItem(k);
    } catch { /* storage unavailable: per-viewer convenience only */ }
  },
};

class ApiError extends Error {
  constructor(status, message, path) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
  }
}

function errorDetail(data, text, res) {
  const d = data && data.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map((x) => {
      const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
      return (loc ? `${loc}: ` : '') + (x.msg || JSON.stringify(x));
    }).join('; ');
  }
  if (d) return JSON.stringify(d);
  const t = (text || '').trim();
  const body = t && t.length < 240 && t !== res.statusText ? ` (${t})` : '';
  return `HTTP ${res.status} ${res.statusText || ''}`.trim() + body;
}

async function api(path, { method = 'GET', params, body } = {}) {
  const url = new URL(API_BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
    }
  }
  const headers = { Accept: 'application/json' };
  if (state.key) headers['X-API-Key'] = state.key;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(url, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, 'Network error: the server could not be reached', path);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) throw new ApiError(res.status, errorDetail(data, text, res), path);
  return data;
}

/** Resolve a promise to {ok, data} | {ok:false, error} so sections can fail independently. */
async function settle(promise) {
  try { return { ok: true, data: await promise }; } catch (error) { return { ok: false, error }; }
}

/** Common query params: run date + warehouse. */
function q(extra = {}) {
  return { date: state.date, warehouse_id: state.wh, ...extra };
}

/* ====================================================== 3. formatters + escape */
const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' };
function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v).replace(/[&<>"'`]/g, (c) => ESC_MAP[c]);
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const toNum = (v) => (isNum(v) ? v : (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) ? Number(v) : null));
const DASH = '—';

/** Indian rupees with lakh / crore. */
function inr(v) {
  const n = toNum(v);
  if (n === null) return DASH;
  const a = Math.abs(n);
  const s = n < 0 ? '-' : '';
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(1)} Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(1)} L`;
  return `${s}₹${a.toLocaleString('en-IN', { maximumFractionDigits: a >= 1000 ? 0 : 2 })}`;
}
/** Exact rupees (unit costs and prices). */
function rupee(v) {
  const n = toNum(v);
  if (n === null) return DASH;
  return `₹${n.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
/** Ratio (0..1) as percent. */
function pct(v) {
  const n = toNum(v);
  return n === null ? DASH : `${(n * 100).toFixed(1)}%`;
}
/** Signed ratio as percent (bias). */
function spct(v) {
  const n = toNum(v);
  if (n === null) return DASH;
  return `${n > 0 ? '+' : ''}${(n * 100).toFixed(1)}%`;
}
/** Value already in percent units. */
function pctv(v) {
  const n = toNum(v);
  return n === null ? DASH : `${n.toFixed(1)}%`;
}
/** Quantity without trailing zeros. */
function qty(v) {
  const n = toNum(v);
  return n === null ? DASH : n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}
function days(v) {
  const n = toNum(v);
  return n === null ? DASH : `${n.toLocaleString('en-IN', { maximumFractionDigits: 1 })} d`;
}
function hours(v) {
  const n = toNum(v);
  return n === null ? DASH : `${n.toLocaleString('en-IN', { maximumFractionDigits: 1 })} h`;
}
function ts(v) {
  if (!v) return DASH;
  return String(v).replace('T', ' ').slice(0, 16);
}
function humanize(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/_/g, ' ').toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}
function truncate(s, n) {
  const t = String(s ?? '');
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

/* ============================================================ 4. HTML blocks */
const STATUS_TONE = {
  SUCCESS: 'ok', APPROVED: 'ok', PUSHED: 'ok', PASS: 'ok', FULFILLED: 'ok', PUBLISHED: 'ok', SENT: 'ok', AUTO_EXECUTED: 'ok', OK: 'ok',
  FAILED: 'bad', FAIL: 'bad', REJECTED: 'bad', CANCELLED: 'bad', ERROR: 'bad', UNAVAILABLE: 'bad',
  WARN: 'warn', DEFERRED: 'warn', HELD: 'warn', PENDING: 'warn', OVERRIDDEN: 'warn', RUNBOOK: 'warn', EXPIRED: 'warn',
  RUNNING: 'info', OPEN: 'info', PROPOSED: 'info', DRAFT: 'info', PURCHASE_TASK: 'info', QUEUED: 'info', LOGGED: 'neutral',
  SHADOW: 'neutral',
};
const AVAIL_TONE = { EASY: 'ok', DIFFICULT: 'warn', SUPPLY_SHORTAGE: 'bad', UNKNOWN: 'neutral' };

function chip(text, tone = 'neutral', title = '') {
  if (text === null || text === undefined || text === '') return `<span class="muted">${DASH}</span>`;
  return `<span class="chip ${esc(tone)}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
}
function statusChip(s) { return chip(s, STATUS_TONE[String(s || '').toUpperCase()] || 'neutral'); }
function prioChip(p) {
  const m = /^P(\d)$/.exec(String(p || ''));
  return m ? chip(p, `p${m[1]}`) : chip(p, 'neutral');
}
function actionChip(a) {
  const i = ACTION_PRIORITY[a];
  return i === undefined ? chip(a, 'neutral') : chip(a, `p${i}`, `P${i}`);
}
const CLASS_SHORT = { CRITICAL_HARD_TO_SOURCE: 'CRITICAL', NON_MOVING: 'NON-MOVING' };
function classChip(c) { return chip(CLASS_SHORT[c] || c, c === 'CRITICAL_HARD_TO_SOURCE' ? 'p0' : 'neutral', c || ''); }
function poPrioChip(p) { return chip(p, PO_PRIORITY_TONE[p] || 'neutral'); }
function chipList(arr, tone = 'neutral') {
  if (!Array.isArray(arr) || !arr.length) return `<span class="muted">${DASH}</span>`;
  return `<span class="chips">${arr.map((x) => chip(x, tone)).join('')}</span>`;
}
function skuLink(id, wh) {
  if (!id) return `<span class="muted">${DASH}</span>`;
  return `<button type="button" class="sku-link" data-sku="${esc(id)}" data-wh="${esc(wh || '')}" title="Open SKU 360">${esc(id)}</button>`;
}
function skuCell(id, name, wh) {
  return `${skuLink(id, wh)}${name ? `<span class="cell-name" title="${esc(name)}">${esc(name)}</span>` : ''}`;
}
function muted(t = DASH) { return `<span class="muted">${esc(t)}</span>`; }

function loadingBlock(text = 'Loading…') {
  return `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;
}
function errorCard(err, title = 'Could not load this view') {
  const status = err && err.status ? ` · HTTP ${err.status}` : '';
  const path = err && err.path ? ` · ${err.path}` : '';
  return `<div class="error-card"><strong>${esc(title)}</strong><span class="muted">${esc(status + path)}</span>
    <code>${esc(err && err.message ? err.message : String(err))}</code></div>`;
}
function pageHead(title, sub = '') {
  return `<div class="page-head"><h1>${esc(title)}</h1>${sub ? `<span class="muted">${esc(sub)}</span>` : ''}</div>`;
}
function section(title, content, { count, extra = '' } = {}) {
  const c = count !== undefined ? `<span class="count">${esc(count)}</span>` : '';
  return `<section class="section"><h2 class="section-title">${esc(title)}${c}${extra ? `<span class="spacer"></span>${extra}` : ''}</h2>${content}</section>`;
}
function tile(label, value, sub = '', tone = '') {
  return `<div class="tile${tone ? ` tone-${esc(tone)}` : ''}"><div class="tile-label" title="${esc(label)}">${esc(label)}</div>
    <div class="tile-value" title="${esc(value)}">${esc(value)}</div>${sub ? `<div class="tile-sub" title="${esc(sub)}">${esc(sub)}</div>` : ''}</div>`;
}
function tiles(list) { return `<div class="tiles">${list.join('')}</div>`; }

/** Generic cell for unknown values. */
function cell(v) {
  if (v === null || v === undefined || v === '') return muted();
  if (typeof v === 'boolean') return v ? chip('yes', 'ok') : chip('no', 'neutral');
  if (isNum(v)) return esc(qty(v));
  if (Array.isArray(v)) {
    if (!v.length) return muted();
    if (v.every((x) => typeof x !== 'object' || x === null)) return chipList(v.map(String));
    return `<code title="${esc(JSON.stringify(v))}">${esc(truncate(JSON.stringify(v), 80))}</code>`;
  }
  if (typeof v === 'object') return `<code title="${esc(JSON.stringify(v))}">${esc(truncate(JSON.stringify(v), 80))}</code>`;
  return esc(v);
}

/**
 * Table builder. cols: [{key, label, num, cls, render(row, i) -> html}]
 * opts: {empty, cls, rowAttrs(row,i) -> attr string, after(row,i) -> extra html rows}
 */
function table(cols, rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(opts.empty || 'Nothing to show')}</div>`;
  const head = cols.map((c) => `<th class="${c.num ? 'num' : ''}"${c.title ? ` title="${esc(c.title)}"` : ''}>${c.labelHtml || esc(c.label)}</th>`).join('');
  const body = rows.map((r, i) => {
    const tds = cols.map((c) => {
      const cls = [c.num ? 'num' : '', c.cls || ''].join(' ').trim();
      const inner = c.render ? c.render(r, i) : cell(r[c.key]);
      return `<td${cls ? ` class="${cls}"` : ''}>${inner}</td>`;
    }).join('');
    return `<tr${opts.rowAttrs ? ` ${opts.rowAttrs(r, i)}` : ''}>${tds}</tr>${opts.after ? opts.after(r, i) : ''}`;
  }).join('');
  return `<div class="tbl-wrap ${esc(opts.cls || '')}"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/** Table with columns derived from the row keys (SKU 360 sections). */
function autoTable(rows, { omit = [], empty = 'No data', render = {} } = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const keys = [];
  for (const r of rows) for (const k of Object.keys(r)) if (!keys.includes(k) && !omit.includes(k)) keys.push(k);
  const cols = keys.map((k) => ({
    key: k,
    label: humanize(k),
    num: rows.some((r) => isNum(r[k])),
    render: render[k] ? (r) => render[k](r[k], r) : undefined,
  }));
  return table(cols, rows, { cls: 'short' });
}

/** Key / value grid, optionally hiding null values. */
function kvGrid(obj, { hideNull = true, format = {} } = {}) {
  if (!obj || typeof obj !== 'object') return muted('No inputs recorded');
  const entries = Object.entries(obj);
  const shown = hideNull ? entries.filter(([, v]) => v !== null && v !== undefined && v !== '') : entries;
  const hidden = entries.length - shown.length;
  const body = shown.map(([k, v]) => `<div><span class="k">${esc(humanize(k))}</span><span class="v">${format[k] ? format[k](v) : cell(v)}</span></div>`).join('');
  return `<div class="kv">${body || muted('All inputs empty')}</div>${hidden ? `<div class="note muted" style="margin-top:6px">${hidden} empty field${hidden > 1 ? 's' : ''} hidden</div>` : ''}`;
}

/** JSON-ish collapsible tree (cycle summary). */
function tree(v, depth = 0) {
  if (v === null || v === undefined) return muted('null');
  if (Array.isArray(v)) {
    if (!v.length) return muted('[]');
    if (v.every((x) => typeof x !== 'object' || x === null)) return esc(v.join(', '));
    return `<ul class="tree">${v.map((x, i) => `<li><span class="k">[${i}]</span> ${tree(x, depth + 1)}</li>`).join('')}</ul>`;
  }
  if (typeof v === 'object') {
    const items = Object.entries(v).map(([k, x]) => {
      const isStatus = /status$/i.test(k) && typeof x === 'string';
      if (x && typeof x === 'object' && Object.keys(x).length) {
        const st = x.status || x._status;
        return `<li><details${depth < 1 ? ' open' : ''}><summary><span class="k">${esc(k)}</span> ${st ? statusChip(st) : ''}</summary>${tree(x, depth + 1)}</details></li>`;
      }
      return `<li><span class="k">${esc(k)}:</span> ${isStatus ? statusChip(x) : (isNum(x) ? esc(qty(x)) : tree(x, depth + 1))}</li>`;
    }).join('');
    return `<ul class="tree">${items}</ul>`;
  }
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return esc(v);
}

/** One-line summary of a small object (job detail). */
function summarize(obj) {
  if (!obj || typeof obj !== 'object') return muted();
  const parts = Object.entries(obj).map(([k, v]) => {
    let s;
    if (v && typeof v === 'object') {
      s = Array.isArray(v) ? v.join(', ') : Object.entries(v).map(([a, b]) => `${a} ${typeof b === 'object' ? JSON.stringify(b) : b}`).join(', ');
      s = `{${s}}`;
    } else s = isNum(v) ? qty(v) : String(v);
    return `${humanize(k)}: ${s}`;
  });
  const full = parts.join(' · ');
  return `<span title="${esc(full)}">${esc(truncate(full, 180))}</span>`;
}

/* ================================================================ 5. charts */
/** Horizontal bars: items [{label, value}] -> one SVG bar per row. */
function hbars(items, fmt = qty, { empty = 'No data' } = {}) {
  const rows = (items || []).filter((i) => isNum(i.value));
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const max = Math.max(...rows.map((i) => Math.abs(i.value)), 0) || 1;
  return `<div class="hbars">${rows.map((i) => {
    const w = Math.max(0, Math.min(100, (Math.abs(i.value) / max) * 100));
    const tone = i.tone ? ` style="fill:var(--${esc(i.tone)})"` : '';
    return `<div class="hbar-row"><div class="hbar-label" title="${esc(i.label)}">${esc(i.label)}</div>
      <svg class="hbar" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" aria-label="${esc(`${i.label}: ${fmt(i.value)}`)}">
        <rect class="hbar-track" x="0" y="0" width="100" height="10"></rect>
        <rect class="hbar-fill" x="0" y="0" width="${w.toFixed(2)}" height="10"${tone}></rect></svg>
      <div class="hbar-val">${esc(fmt(i.value))}</div></div>`;
  }).join('')}</div>`;
}

/** Column chart: items [{label, value, sub}] in a scaled SVG. */
function columns(items, fmt = qty, { empty = 'No data' } = {}) {
  const rows = (items || []).filter((i) => isNum(i.value));
  if (!rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const slot = 76;
  const W = Math.max(360, rows.length * slot + 20);
  const H = 190;
  const top = 22;
  const base = 150;
  const max = Math.max(...rows.map((i) => i.value), 0) || 1;
  const bw = Math.min(44, slot - 22);
  const x0 = (W - rows.length * slot) / 2;
  const bars = rows.map((i, k) => {
    const h = Math.max(1, (i.value / max) * (base - top));
    const cx = x0 + k * slot + slot / 2;
    return `<g><title>${esc(`${i.label}: ${fmt(i.value)}${i.sub ? ` (${i.sub})` : ''}`)}</title>
      <rect class="col" x="${(cx - bw / 2).toFixed(1)}" y="${(base - h).toFixed(1)}" width="${bw}" height="${h.toFixed(1)}" rx="3"></rect>
      <text class="val" x="${cx.toFixed(1)}" y="${(base - h - 6).toFixed(1)}" text-anchor="middle">${esc(fmt(i.value))}</text>
      <text class="lbl" x="${cx.toFixed(1)}" y="${base + 16}" text-anchor="middle">${esc(i.label)}</text>
      ${i.sub ? `<text class="sub" x="${cx.toFixed(1)}" y="${base + 30}" text-anchor="middle">${esc(i.sub)}</text>` : ''}</g>`;
  }).join('');
  return `<svg class="cols-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Column chart">
    <line class="axis" x1="10" x2="${W - 10}" y1="${base}" y2="${base}"></line>${bars}</svg>`;
}

/** Tiny benefit-vs-cost split bar. */
function benefitCostBar(benefit, cost) {
  const b = Math.max(0, toNum(benefit) || 0);
  const c = Math.max(0, toNum(cost) || 0);
  const t = b + c || 1;
  const bw = (b / t) * 100;
  return `<span class="bc-bar" title="${esc(`Benefit ${inr(b)} vs cost ${inr(c)} per month`)}"><svg viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">
    <rect class="b" x="0" y="0" width="${bw.toFixed(2)}" height="8"></rect><rect class="c" x="${bw.toFixed(2)}" y="0" width="${(100 - bw).toFixed(2)}" height="8"></rect></svg></span>`;
}

/* =========================================================== 6. UI services */
function toast(message, kind = 'error', title = '') {
  const root = document.getElementById('toasts');
  if (!root) return;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  const t = title || (kind === 'error' ? 'Error' : kind === 'ok' ? 'Done' : 'Info');
  el.innerHTML = `<div class="t-title">${esc(t)}</div><div>${esc(message)}</div>`;
  el.addEventListener('click', () => el.remove());
  root.appendChild(el);
  setTimeout(() => el.remove(), kind === 'error' ? 9000 : 5000);
  while (root.children.length > 5) root.firstChild.remove();
}
/** Error message with the HTTP status appended once. */
function errText(err) {
  const msg = err && err.message ? err.message : String(err);
  return err && err.status && !msg.startsWith('HTTP') ? `${msg} (HTTP ${err.status})` : msg;
}
function toastError(err, title) {
  toast(err && err.message ? err.message : String(err), 'error', title || (err && err.status ? `Request failed (HTTP ${err.status})` : 'Error'));
}

function setBusy(btn, busy) {
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle('busy', busy);
}

/**
 * Small modal form. fields: [{name, label, type: text|textarea|select, options, value, minlength, required, placeholder}]
 * Resolves to an object of values, or null when cancelled.
 */
function modalForm({ title, text = '', fields = [], submit = 'Submit', danger = false }) {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root');
    const prevFocus = document.activeElement;
    const fieldHtml = fields.map((f) => {
      const req = f.required ? ' required' : '';
      const min = f.minlength ? ` minlength="${f.minlength}"` : '';
      let input;
      if (f.type === 'select') {
        input = `<select name="${esc(f.name)}"${req}>${f.options.map((o) => `<option value="${esc(o.value)}"${o.value === f.value ? ' selected' : ''}>${esc(o.label)}</option>`).join('')}</select>`;
      } else if (f.type === 'textarea') {
        input = `<textarea name="${esc(f.name)}" rows="3"${req}${min} placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>`;
      } else {
        input = `<input type="text" name="${esc(f.name)}" value="${esc(f.value || '')}"${req}${min} placeholder="${esc(f.placeholder || '')}">`;
      }
      return `<label>${esc(f.label)}${input}</label>`;
    }).join('');
    root.innerHTML = `<div class="modal-scrim"><form class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ''}${fieldHtml}
      <div class="btn-row"><button type="button" class="btn" data-modal="cancel">Cancel</button>
      <button type="submit" class="btn ${danger ? 'danger' : 'primary'}">${esc(submit)}</button></div></form></div>`;
    const form = root.querySelector('form');
    const close = (val) => {
      root.innerHTML = '';
      document.removeEventListener('keydown', onKey);
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      resolve(val);
    };
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    root.querySelector('[data-modal="cancel"]').addEventListener('click', () => close(null));
    root.querySelector('.modal-scrim').addEventListener('click', (e) => { if (e.target.classList.contains('modal-scrim')) close(null); });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const out = {};
      for (const f of fields) out[f.name] = form.elements[f.name].value.trim();
      close(out);
    });
    const first = form.querySelector('input, textarea, select');
    if (first) first.focus();
  });
}

function askReason(title, text) {
  return modalForm({
    title, text, submit: 'Reject', danger: true,
    fields: [{ name: 'reason', label: 'Reason (min 3 characters)', type: 'textarea', required: true, minlength: 3 }],
  }).then((v) => (v ? v.reason : null));
}

/* ===================================================== 7. tab renderers */

/* ----- Overview ----- */
async function viewOverview() {
  const res = await settle(api('/dashboard/kpis', { params: q() }));
  if (!res.ok) {
    // KPI endpoint failed: still show platform health from the admin endpoints.
    const [jobs, dq] = await Promise.all([settle(api('/admin/jobs', { params: { date: state.date } })), settle(api('/admin/data-quality', { params: { date: state.date } }))]);
    return pageHead('Overview', `Run date ${state.date}`) + errorCard(res.error, 'KPI summary unavailable')
      + `<div class="grid-2">${section('Last jobs', jobs.ok ? jobsTable(jobs.data) : errorCard(jobs.error))}
         ${section('Data quality failures', dq.ok ? dqFailures(dq.data.filter((d) => !d.passed)) : errorCard(dq.error))}</div>`;
  }
  const k = res.data;
  const a = k.availability || {};
  const inv = k.inventory || {};
  const m = k.margin || {};
  const mh = k.model_health || {};
  const p = k.platform || {};
  const fill = toNum(a.fill_rate_line_30d);
  const dqRate = toNum(p.data_quality_pass_rate);
  const kp = tiles([
    tile('Fill rate (line, 30d)', pct(a.fill_rate_line_30d), `${qty(a.order_lines_30d)} order lines`, fill !== null && fill < 0.9 ? 'warn' : ''),
    tile('Bounce rate (30d)', pct(a.bounce_rate_30d), `${qty(a.final_bounces_30d)} final bounces`),
    tile('Bounce recovery rate', pct(a.bounce_recovery_rate_30d), 'recovered / (recovered + final)'),
    tile('Revenue lost (30d)', inr(a.revenue_lost_30d), 'final bounces', 'bad'),
    tile('Retailers affected (30d)', qty(a.retailers_affected_30d), 'with a final bounce'),
    tile('Stock value', inr(inv.stock_value), inv.working_capital_pct_of_30d_turnover != null ? `${pct(inv.working_capital_pct_of_30d_turnover)} of 30d turnover` : ''),
    tile('Inventory days', days(inv.inventory_days), 'stock value / daily COGS'),
    tile('Near-expiry value (90d)', inr(inv.near_expiry_value_90d), 'batches expiring ≤ 90 days', toNum(inv.near_expiry_value_90d) > 0 ? 'warn' : ''),
    tile('Gross margin (30d)', pctv(m.gross_margin_pct_30d), `on ${inr(m.revenue_30d)} revenue`),
    tile('PO acceptance rate', pct(mh.po_acceptance_rate_30d), 'reviewed POs not rejected'),
    tile('Override rate', pct(mh.override_rate_30d), `${qty(mh.class_switches_today)} class switches today`),
    tile('DQ pass rate', pct(p.data_quality_pass_rate), `${(p.data_quality_failures || []).length} failing checks`, dqRate !== null && dqRate < 1 ? 'warn' : 'ok'),
  ]);
  const acc = table([
    { key: 'sku_class', label: 'Class', render: (r) => classChip(r.sku_class) },
    { key: 'method', label: 'Method' },
    { key: 'role', label: 'Role', render: (r) => chip(r.role, r.role === 'champion' ? 'ok' : r.role === 'live' ? 'info' : 'neutral') },
    { key: 'wape', label: 'WAPE', num: true, render: (r) => esc(pct(r.wape)) },
    { key: 'bias', label: 'Bias', num: true, render: (r) => esc(spct(r.bias)) },
  ], mh.forecast_accuracy || [], { empty: 'No forecast accuracy recorded for this date', cls: 'short' });
  return pageHead('Overview', `Run date ${k.run_date} · ${k.warehouse_id === 'ALL' ? 'All warehouses' : k.warehouse_id}`)
    + kp
    + `<div class="grid-2">${section('Forecast accuracy', acc, { count: (mh.forecast_accuracy || []).length })}
       ${section('Data quality failures', dqFailures(p.data_quality_failures || []), { count: (p.data_quality_failures || []).length })}</div>`
    + section('Last jobs', jobsTable(p.last_jobs || []), { count: (p.last_jobs || []).length });
}

function jobsTable(rows) {
  return table([
    { key: 'job', label: 'Job', render: (r) => `<code>${esc(r.job)}</code>` },
    { key: 'status', label: 'Status', render: (r) => statusChip(r.status) },
    { key: 'started_at', label: 'Started', render: (r) => esc(ts(r.started_at)) },
    { key: 'finished_at', label: 'Finished', render: (r) => esc(ts(r.finished_at)) },
    { key: 'detail', label: 'Detail', cls: 'clip', render: (r) => summarize(r.detail) },
  ], rows, { empty: 'No jobs recorded for this date', cls: 'short' });
}
function dqFailures(rows) {
  if (!rows.length) return '<div class="empty">All data-quality checks passed</div>';
  return table([
    { key: 'check', label: 'Check', render: (r) => `<code>${esc(r.check)}</code>` },
    { key: 'severity', label: 'Severity', render: (r) => chip(r.severity, r.severity === 'ERROR' ? 'bad' : 'warn') },
    { key: 'detail', label: 'Detail', cls: 'wrap', render: (r) => (r.detail ? esc(r.detail) : muted()) },
  ], rows, { cls: 'short' });
}

/* ----- Inventory ----- */
async function viewInventory() {
  const d = await api('/dashboard/inventory', { params: q() });
  const byClass = d.by_class || [];
  const total = byClass.reduce((s, c) => s + (toNum(c.stock_value) || 0), 0);
  const t = tiles([
    tile('Working capital blocked', inr(d.working_capital_blocked), 'total stock at cost'),
    tile('In slow + non-moving', inr(d.working_capital_in_slow_nonmoving), total ? `${pct((d.working_capital_in_slow_nonmoving || 0) / total)} of class-tagged stock` : '', 'warn'),
    tile('On compliance hold', inr(d.on_hold_value), 'batches flagged on hold', toNum(d.on_hold_value) > 0 ? 'bad' : ''),
    tile('Near-expiry (90d)', inr(d.near_expiry_value_90d), 'value at cost', toNum(d.near_expiry_value_90d) > 0 ? 'warn' : ''),
    tile('Near-expiry (180d)', inr(d.near_expiry_value_180d), 'value at cost'),
  ]);
  const classTable = table([
    { key: 'class', label: 'Class', render: (r) => classChip(r.class) },
    { key: 'skus', label: 'SKUs', num: true, render: (r) => esc(qty(r.skus)) },
    { key: 'skus_with_stock', label: 'With stock', num: true, render: (r) => esc(qty(r.skus_with_stock)) },
    { key: 'stock_value', label: 'Stock value', num: true, render: (r) => esc(inr(r.stock_value)) },
    { key: 'share', label: 'Share', num: true, render: (r) => esc(total ? pct((r.stock_value || 0) / total) : DASH) },
  ], byClass, { empty: 'No classification for this date', cls: 'short' });
  const classChart = hbars(byClass.map((c) => ({ label: humanize(c.class), value: c.stock_value })), inr);
  const ageChart = columns((d.ageing || []).map((b) => ({ label: `${b.bucket} d`, value: b.value, sub: `${qty(b.batches)} batches` })), inr);
  const near = table([
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'batch', label: 'Batch', render: (r) => `<code>${esc(r.batch)}</code>` },
    { key: 'expiry', label: 'Expiry' },
    { key: 'days_to_expiry', label: 'Days left', num: true, render: (r) => chip(qty(r.days_to_expiry), r.days_to_expiry <= 60 ? 'bad' : r.days_to_expiry <= 90 ? 'warn' : 'neutral') },
    { key: 'qty', label: 'Qty', num: true, render: (r) => esc(qty(r.qty)) },
    { key: 'value', label: 'Value', num: true, render: (r) => esc(inr(r.value)) },
  ], d.near_expiry || [], { empty: 'No batches expiring within 180 days' });
  return pageHead('Inventory', `Run date ${d.run_date}`) + t
    + `<div class="grid-2">${section('Stock value by class', `<div class="card">${classChart}</div>`)}
       ${section('Ageing (days since inward)', `<div class="card">${ageChart}</div>`)}</div>`
    + section('By class', classTable, { count: byClass.length })
    + section('Near expiry (≤ 180 days)', near, { count: (d.near_expiry || []).length });
}

/* ----- Bounce ----- */
async function viewBounce() {
  const d = await api('/dashboard/bounce', { params: q() });
  const t = d.today || {};
  const reasons = Object.entries(t.by_reason || {}).sort((a, b) => b[1] - a[1]);
  const todayTiles = tiles([
    tile('Bounces', qty(t.bounces), `on ${t.date || DASH}`),
    tile('Final (lost)', qty(t.final), `${qty(t.pending)} pending`, toNum(t.final) > 0 ? 'bad' : ''),
    tile('Recovered', qty(t.recovered), t.bounces ? `${pct((t.recovered || 0) / t.bounces)} of bounces` : '', 'ok'),
    tile('Revenue lost', inr(t.revenue_lost), 'today', 'bad'),
    tile('Retailers affected', qty(t.retailers_affected), 'today'),
    tile('Revenue lost (90d)', inr(d.revenue_lost_90d), 'from bounce profiles'),
  ]);
  const reasonChart = hbars(reasons.map(([k, v]) => ({ label: humanize(k), value: v })), qty, { empty: 'No bounces today' });
  const mix = Object.entries(d.decision_mix || {}).sort((a, b) => b[1] - a[1]);
  const mixChart = hbars(mix.map(([k, v]) => ({ label: humanize(k), value: v })), qty, { empty: 'No stock decisions for this date' });
  const top = table([
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'bounces_30', label: '30d', num: true, render: (r) => esc(qty(r.bounces_30)) },
    { key: 'bounces_60', label: '60d', num: true, render: (r) => esc(qty(r.bounces_60)) },
    { key: 'bounces_90', label: '90d', num: true, render: (r) => `<strong>${esc(qty(r.bounces_90))}</strong>` },
    { key: 'retailers', label: 'Retailers', num: true, render: (r) => esc(qty(r.retailers)) },
    { key: 'value_lost_90', label: 'Value lost 90d', num: true, render: (r) => esc(inr(r.value_lost_90)) },
    { key: 'pattern', label: 'Pattern', render: (r) => chip(r.pattern, r.pattern === 'REGULAR' ? 'info' : 'neutral') },
    { key: 'spread', label: 'Spread', render: (r) => chip(r.spread, 'neutral') },
    { key: 'external_availability', label: 'External availability', render: (r) => chip(r.external_availability, AVAIL_TONE[r.external_availability] || 'neutral') },
    { key: 'recovery_rate', label: 'Recovery', num: true, render: (r) => esc(pct(r.recovery_rate)) },
  ], d.top_bounced || [], { empty: 'No bounce profiles for this date' });
  const stock = table([
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'decision', label: 'Decision', render: (r) => chip(r.decision, r.decision === 'STOCK' ? 'p3' : 'info') },
    { key: 'target_stock_qty', label: 'Target qty', num: true, render: (r) => esc(qty(r.target_stock_qty)) },
    { key: 'benefit_per_month', label: 'Benefit / mo', num: true, render: (r) => `<span style="color:var(--ok)">${esc(inr(r.benefit_per_month))}</span>` },
    { key: 'cost_per_month', label: 'Cost / mo', num: true, render: (r) => `<span style="color:var(--bad)">${esc(inr(r.cost_per_month))}</span>` },
    { key: 'net', label: 'Net / mo', num: true, render: (r) => `<strong>${esc(inr((r.benefit_per_month || 0) - (r.cost_per_month || 0)))}</strong>` },
    { key: 'bc', label: 'Benefit vs cost', render: (r) => benefitCostBar(r.benefit_per_month, r.cost_per_month) },
    { key: 'reason', label: 'Reason', cls: 'wrap', render: (r) => (r.reason ? esc(r.reason) : muted()) },
  ], d.recommended_for_stocking || [], { empty: 'No SKUs recommended for stocking' });
  return pageHead('Bounce intelligence', `Run date ${d.run_date}`)
    + section("Today's bounces", todayTiles, { count: t.date || '' })
    + `<div class="grid-2">${section('Bounces by reason (today)', `<div class="card">${reasonChart}</div>`)}
       ${section('Stock decision mix', `<div class="card">${mixChart}</div>`)}</div>`
    + section('Top bounced SKUs', top, { count: (d.top_bounced || []).length })
    + section('Recommended for stocking', stock, { count: (d.recommended_for_stocking || []).length });
}

/* ----- Purchase ----- */
async function viewPurchase() {
  const [dash, pos] = await Promise.all([
    settle(api('/dashboard/purchase', { params: q() })),
    settle(api('/po-drafts', { params: q() })),
  ]);
  if (!pos.ok) throw pos.error;
  const P = state.po;
  P.dash = dash.ok ? dash.data : null;
  P.lines = {};
  if (dash.ok) for (const l of dash.data.lines || []) P.lines[l.po_draft_id] = l;
  P.rows = (pos.data || []).slice().sort((a, b) => (PO_PRIORITY_RANK[a.priority] ?? 9) - (PO_PRIORITY_RANK[b.priority] ?? 9) || (b.line_value || 0) - (a.line_value || 0));
  const ids = new Set(P.rows.map((r) => r.po_draft_id));
  for (const id of [...P.selected]) if (!ids.has(id)) P.selected.delete(id);

  let head;
  if (dash.ok) {
    const d = dash.data;
    const vp = d.po_value_by_priority || {};
    head = tiles([
      tile('Tomorrow demand', `${qty(d.tomorrow_demand_units)} units`, `${inr(d.tomorrow_demand_value)} · ${qty(d.forecast_skus)} SKUs`),
      tile('Next 2 days demand', `${qty(d.two_day_demand_units)} units`, inr(d.two_day_demand_value)),
      tile('Safety stock', `${qty(d.safety_stock_units)} units`, 'buffer policy total'),
      tile('PO lines', qty(d.po_lines), `${inr(d.po_value)} total value`),
      tile('High priority', inr(vp.HIGH), 'PO value', 'bad'),
      tile('Medium priority', inr(vp.MEDIUM), 'PO value', 'warn'),
      tile('Low priority', inr(vp.LOW), 'PO value'),
      tile('No gated supplier', qty(d.lines_without_gated_supplier), 'lines need a supplier', toNum(d.lines_without_gated_supplier) > 0 ? 'warn' : ''),
    ]);
  } else {
    head = errorCard(dash.error, 'Purchase summary unavailable (tiles and SKU names hidden); PO drafts loaded directly');
  }
  const statuses = [...new Set(P.rows.map((r) => r.status))].sort();
  const toolbar = `<div class="toolbar">
    <label>Status<select data-po-filter="status"><option value="">All</option>${statuses.map((s) => `<option value="${esc(s)}"${P.status === s ? ' selected' : ''}>${esc(s)}</option>`).join('')}</select></label>
    <label>Priority<select data-po-filter="priority"><option value="">All</option>${['HIGH', 'MEDIUM', 'LOW'].map((s) => `<option value="${s}"${P.priority === s ? ' selected' : ''}>${s}</option>`).join('')}</select></label>
    <span class="spacer"></span>
    <button type="button" class="btn primary" data-act="po-bulk" id="po-bulk-btn"${P.selected.size ? '' : ' disabled'}>Approve selected (${P.selected.size})</button>
  </div>`;
  return pageHead('Purchase', `Run date ${state.date}${isShadow() ? ' · shadow mode: approvals are recorded but not pushed to the ERP' : ''}`)
    + head + section('Suggested purchase orders', toolbar + `<div id="po-table">${poTable()}</div>`, { count: `${P.rows.length} drafts` });
}

function poFiltered() {
  const P = state.po;
  return P.rows.filter((r) => (!P.status || r.status === P.status) && (!P.priority || r.priority === P.priority));
}
function poQty(r) {
  const e = state.po.edits[r.po_draft_id];
  return e !== undefined ? e : r.qty;
}
function poTable() {
  const P = state.po;
  const rows = poFiltered();
  const selectable = rows.filter((r) => OPEN_PO_STATES.includes(r.status));
  const allSel = selectable.length > 0 && selectable.every((r) => P.selected.has(r.po_draft_id));
  return table([
    {
      key: 'sel', labelHtml: `<input type="checkbox" data-po-selall aria-label="Select all"${allSel ? ' checked' : ''}${selectable.length ? '' : ' disabled'}>`,
      render: (r) => (OPEN_PO_STATES.includes(r.status) ? `<input type="checkbox" data-po-sel="${esc(r.po_draft_id)}" aria-label="Select ${esc(r.sku_id)}"${P.selected.has(r.po_draft_id) ? ' checked' : ''}>` : ''),
    },
    { key: 'priority', label: 'Priority', render: (r) => poPrioChip(r.priority) },
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, (P.lines[r.po_draft_id] || {}).name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'sku_class', label: 'Class', render: (r) => classChip(r.sku_class) },
    { key: 'current_stock', label: 'Stock', num: true, render: (r) => esc(qty(r.current_stock)) },
    { key: 'open_po_qty', label: 'Open PO', num: true, render: (r) => esc(qty(r.open_po_qty)) },
    { key: 'forecast_1d', label: 'Fcst 1d', num: true, render: (r) => esc(qty(r.forecast_1d)) },
    { key: 'forecast_2d', label: 'Fcst 2d', num: true, render: (r) => esc(qty(r.forecast_2d)) },
    { key: 'buffer_qty', label: 'Buffer', num: true, render: (r) => esc(qty(r.buffer_qty)) },
    {
      key: 'qty', label: 'Suggested qty', num: true,
      render: (r) => (OPEN_PO_STATES.includes(r.status)
        ? `<input type="number" min="0" step="1" value="${esc(poQty(r))}" data-po-qty="${esc(r.po_draft_id)}" aria-label="Quantity for ${esc(r.sku_id)}"${state.po.edits[r.po_draft_id] !== undefined ? ' class="edited"' : ''}>`
        : esc(qty(r.qty))),
    },
    { key: 'unit_cost', label: 'Unit cost', num: true, render: (r) => esc(rupee(r.unit_cost)) },
    { key: 'line_value', label: 'Value', num: true, render: (r) => `<span data-po-val="${esc(r.po_draft_id)}">${esc(inr((toNum(poQty(r)) || 0) * (r.unit_cost || 0)))}</span>` },
    { key: 'supplier_id', label: 'Supplier', render: (r) => { const s = (P.lines[r.po_draft_id] || {}).supplier; return r.supplier_id ? `${esc(s || r.supplier_id)}${s ? ` <span class="muted">${esc(r.supplier_id)}</span>` : ''}` : chip('none gated', 'warn'); } },
    { key: 'reason_code', label: 'Reason', render: (r) => `<code>${esc(r.reason_code)}</code>` },
    { key: 'constraints_applied', label: 'Constraints', render: (r) => chipList(r.constraints_applied, 'neutral') },
    { key: 'status', label: 'Status', render: (r) => `${statusChip(r.status)}${r.erp_ref ? ` <span class="muted" title="ERP ref">${esc(r.erp_ref)}</span>` : ''}${r.note ? ` <span class="muted" title="${esc(r.note)}">ⓘ</span>` : ''}` },
    {
      key: 'actions', label: 'Actions',
      render: (r) => (OPEN_PO_STATES.includes(r.status)
        ? `<span class="btn-row"><button type="button" class="btn sm ok" data-act="po-approve" data-id="${esc(r.po_draft_id)}">Approve</button>
           <button type="button" class="btn sm danger" data-act="po-reject" data-id="${esc(r.po_draft_id)}">Reject</button></span>`
        : muted()),
    },
  ], rows, { empty: P.rows.length ? 'No drafts match the filters' : 'No PO drafts for this date', cls: 'tall' });
}
function refreshPoTable() {
  const el = document.getElementById('po-table');
  if (el) el.innerHTML = poTable();
  updateBulkButton();
}
function updateBulkButton() {
  const b = document.getElementById('po-bulk-btn');
  if (b) { b.disabled = state.po.selected.size === 0; b.textContent = `Approve selected (${state.po.selected.size})`; }
}

/* ----- Margin ----- */
async function viewMargin() {
  const d = await api('/dashboard/margin', { params: q() });
  const t = tiles([
    tile('Leakage on fast movers (30d)', inr(d.margin_leakage_fast_30d), 'excess discount given', toNum(d.margin_leakage_fast_30d) > 0 ? 'bad' : ''),
    tile('Slow-moving eligible', inr(d.slow_moving_eligible_value), 'special-lot offers at cost'),
    tile('Liquidation opportunity', inr(d.liquidation_opportunity_value), 'liquidation tiers at cost', 'warn'),
    tile('Supplier returns', inr(d.return_to_supplier_value), `${(d.returns || []).length} return proposals`),
    tile('Price tests proposed', qty(d.price_tests_proposed), Object.entries(d.offers_by_status || {}).map(([k, v]) => `${humanize(k)} ${v}`).join(' · ')),
  ]);
  const leak = table([
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'current_discount', label: 'Current disc.', num: true, render: (r) => esc(pctv(r.current_discount)) },
    { key: 'recommended_discount', label: 'Recommended', num: true, render: (r) => esc(pctv(r.recommended_discount)) },
    { key: 'leakage_30d', label: 'Leakage 30d', num: true, render: (r) => `<strong>${esc(inr(r.leakage_30d))}</strong>` },
  ], d.leakage || [], { empty: 'No margin leakage detected' });
  const lotCols = [
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'batch', label: 'Batch', render: (r) => (r.batch ? `<code>${esc(r.batch)}</code>` : muted()) },
    { key: 'tier', label: 'Tier', render: (r) => chip(r.tier, String(r.tier || '').startsWith('LIQUIDATE') ? 'p1' : 'p4') },
    { key: 'current_discount', label: 'Normal disc.', num: true, render: (r) => esc(pctv(r.current_discount)) },
    { key: 'recommended_discount', label: 'Offer disc.', num: true, render: (r) => `<strong>${esc(pctv(r.recommended_discount))}</strong>` },
    { key: 'term_flag', label: 'Terms', render: (r) => chip(r.term_flag === 'SPECIAL_NON_RETURNABLE' ? 'Non-returnable' : humanize(r.term_flag), r.term_flag === 'SPECIAL_NON_RETURNABLE' ? 'warn' : 'neutral') },
    { key: 'qty', label: 'Qty', num: true, render: (r) => esc(qty(r.qty)) },
    { key: 'value_at_cost', label: 'Value at cost', num: true, render: (r) => esc(inr(r.value_at_cost)) },
    { key: 'net_price', label: 'Net price', num: true, render: (r) => esc(rupee(r.net_price)) },
    { key: 'status', label: 'Status', render: (r) => statusChip(r.status) },
    { key: 'reason', label: 'Reason', cls: 'wrap', render: (r) => (r.reason ? esc(r.reason) : muted()) },
    { key: 'actions', label: 'Actions', render: offerActions },
  ];
  const returns = table([
    { key: 'sku_id', label: 'SKU', render: (r) => skuLink(r.sku_id, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'batch', label: 'Batch', render: (r) => (r.batch ? `<code>${esc(r.batch)}</code>` : muted()) },
    { key: 'qty', label: 'Qty', num: true, render: (r) => esc(qty(r.qty)) },
    { key: 'reason', label: 'Reason', cls: 'wrap', render: (r) => (r.reason ? esc(r.reason) : muted()) },
    { key: 'actions', label: 'Actions', render: (r) => offerActions({ ...r, status: 'PROPOSED' }) },
  ], d.returns || [], { empty: 'No supplier return proposals', cls: 'short' });
  return pageHead('Margin', `Run date ${d.run_date}`) + t
    + section('Margin leakage on fast movers', leak, { count: (d.leakage || []).length })
    + section('Special lots (slow-moving)', table(lotCols, d.special_lots || [], { empty: 'No special-lot offers' }), { count: (d.special_lots || []).length })
    + section('Liquidation lots', table(lotCols, d.liquidation_lots || [], { empty: 'No liquidation offers' }), { count: (d.liquidation_lots || []).length })
    + section('Supplier returns', returns, { count: (d.returns || []).length });
}
function offerActions(r) {
  if (r.status !== 'PROPOSED') return muted();
  return `<span class="btn-row"><button type="button" class="btn sm ok" data-act="offer-approve" data-id="${esc(r.offer_id)}">Approve</button>
    <button type="button" class="btn sm danger" data-act="offer-reject" data-id="${esc(r.offer_id)}">Reject</button></span>`;
}

/* ----- Decisions ----- */
async function viewDecisions() {
  const D = state.dec;
  const d = await api('/decisions', { params: q({ action: D.action, limit: 200 }) });
  D.items = {};
  for (const it of d.items || []) D.items[it.id] = it;
  const counts = d.by_action || {};
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  const chips = `<div class="filter-bar" role="group" aria-label="Filter by action">
    <button type="button" class="fchip all" data-dec-action="" aria-pressed="${D.action === ''}"><span class="dot"></span>All<span class="n">${esc(qty(total))}</span></button>
    ${ACTIONS.map((a, i) => `<button type="button" class="fchip p${i}" data-dec-action="${a}" aria-pressed="${D.action === a}" title="P${i}">
      <span class="dot"></span>${esc(humanize(a))}<span class="n">${esc(qty(counts[a] || 0))}</span></button>`).join('')}
  </div>`;
  const cols = [
    { key: 'priority', label: 'Pri', render: (r) => prioChip(r.priority) },
    { key: 'sku_id', label: 'SKU', render: (r) => skuLink(r.sku_id, r.warehouse_id) },
    { key: 'name', label: 'Name', render: (r) => (r.name ? `<span class="cell-name" title="${esc(r.name)}">${esc(r.name)}</span>` : muted()) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'sku_class', label: 'Class', render: (r) => classChip(r.sku_class) },
    { key: 'action', label: 'Action', render: (r) => `${actionChip(r.action)}${r.override_action ? ` → ${actionChip(r.override_action)}` : ''}` },
    { key: 'reason_text', label: 'Reason', cls: 'wrap', render: (r) => esc(r.reason_text) },
    { key: 'secondary_tags', label: 'Tags', cls: 'wrap-sm', render: (r) => chipList(r.secondary_tags) },
    { key: 'status', label: 'Status', render: (r) => statusChip(r.status) },
  ];
  const rows = d.items || [];
  const tbl = table(cols, rows, {
    empty: 'No decisions for this date / filter',
    cls: 'tall',
    rowAttrs: (r) => `class="clickable${D.open.has(r.id) ? ' is-open' : ''}" data-dec-row="${esc(r.id)}" tabindex="0" aria-expanded="${D.open.has(r.id)}"`,
    after: (r) => `<tr class="detail-row" data-dec-detail="${esc(r.id)}"${D.open.has(r.id) ? '' : ' hidden'}><td colspan="${cols.length}">${D.open.has(r.id) ? decisionDetail(r) : ''}</td></tr>`,
  });
  const shown = rows.length < d.total ? `${rows.length} of ${qty(d.total)}` : `${qty(d.total)}`;
  return pageHead('Decisions', `Run date ${d.run_date} · one primary action per SKU × warehouse`)
    + chips + section('Decision log', tbl, { count: `${shown} decisions · click a row for inputs` });
}

const SNAPSHOT_FORMAT = {
  stock_value: (v) => esc(inr(v)), leakage_30d: (v) => esc(inr(v)), current_discount: (v) => esc(pctv(v)),
  recommended_discount: (v) => esc(pctv(v)), service_level: (v) => esc(pct(v)),
};

function decisionDetail(r) {
  const D = state.dec;
  const ex = D.explain[r.id];
  const override = r.overridden_by ? `<div class="panel warn"><div class="panel-head">Overridden ${statusChip('OVERRIDDEN')}</div>
    <div>${esc(r.overridden_by)} changed to ${actionChip(r.override_action)}: ${esc(r.override_reason || '')}</div></div>` : '';
  return `<div class="detail-inner"><div class="note muted" style="margin-bottom:6px">Reason code <code>${esc(r.reason_code)}</code> · version <code>${esc(r.version)}</code> · mode ${esc(r.autonomy_mode)}</div>
    ${kvGrid(r.inputs_snapshot, { format: SNAPSHOT_FORMAT })}
    ${override}
    <div class="btn-row" style="margin-top:10px">
      <button type="button" class="btn sm" data-act="explain" data-id="${esc(r.id)}" data-lang="en">Explain</button>
      <button type="button" class="btn sm" data-act="explain" data-id="${esc(r.id)}" data-lang="te" lang="te">తెలుగు</button>
      <button type="button" class="btn sm" data-act="override" data-id="${esc(r.id)}">Override</button>
    </div>
    <div data-explain="${esc(r.id)}">${ex ? explainHtml(ex) : ''}</div></div>`;
}
function explainHtml(ex) {
  if (ex.error) return `<div class="panel bad"><div class="panel-head">Explanation unavailable</div><div>${esc(ex.error)}</div></div>`;
  const src = ex.source === 'claude' ? chip('Claude', 'info') : chip(ex.source || 'template', 'neutral');
  return `<div class="panel${ex.source === 'claude' ? '' : ' warn'}"><div class="panel-head">Explanation ${src} ${chip(ex.lang === 'te' ? 'తెలుగు' : 'English', 'neutral')}</div>
    <p class="pre"${ex.lang === 'te' ? ' lang="te"' : ''}>${esc(ex.explanation)}</p>${ex.note ? `<div class="note muted" style="margin-top:6px">${esc(ex.note)}</div>` : ''}</div>`;
}

/* ----- Sourcing ----- */
async function viewSourcing() {
  const S = state.sourcing;
  const res = await settle(api('/sourcing/queue', { params: { status: S.status, warehouse_id: state.wh, limit: 200 } }));
  const filter = `<div class="toolbar"><label>Status<select data-src-filter>
      ${[['', 'Active (open / held / purchase task)'], ['OPEN', 'OPEN'], ['HELD', 'HELD'], ['PURCHASE_TASK', 'PURCHASE_TASK'], ['FULFILLED', 'FULFILLED'], ['FAILED', 'FAILED'], ['CANCELLED', 'CANCELLED']]
        .map(([v, l]) => `<option value="${v}"${S.status === v ? ' selected' : ''}>${esc(l)}</option>`).join('')}
    </select></label></div>`;
  const queue = res.ok ? table([
    { key: 'status', label: 'Status', render: (r) => statusChip(r.status) },
    { key: 'sku_id', label: 'SKU', render: (r) => skuCell(r.sku_id, r.name, r.warehouse_id) },
    { key: 'warehouse_id', label: 'WH' },
    { key: 'retailer_id', label: 'Retailer', render: (r) => (r.retailer_id ? esc(r.retailer_id) : muted()) },
    { key: 'qty', label: 'Qty', num: true, render: (r) => esc(qty(r.qty)) },
    { key: 'value', label: 'Value', num: true, render: (r) => esc(inr(r.value)) },
    { key: 'priority_score', label: 'Priority score', num: true, render: (r) => esc(qty(r.priority_score)) },
    { key: 'eta_hours', label: 'ETA', num: true, render: (r) => esc(hours(r.eta_hours)) },
    { key: 'failure_reason', label: 'Failure', render: (r) => (r.failure_reason ? chip(r.failure_reason, 'bad') : muted()) },
    { key: 'best_offer', label: 'Best offer', cls: 'wrap-sm', render: (r) => bestOffer(r.best_offer) },
    { key: 'created_at', label: 'Created', render: (r) => esc(ts(r.created_at)) },
    {
      key: 'actions', label: 'Actions',
      render: (r) => (ACTIVE_SOURCING.includes(r.status) ? `<span class="btn-row">
        <button type="button" class="btn sm" data-act="src" data-op="confirm_purchase" data-id="${esc(r.id)}">Confirm purchase</button>
        <button type="button" class="btn sm ok" data-act="src" data-op="fulfilled" data-id="${esc(r.id)}">Fulfilled</button>
        <button type="button" class="btn sm" data-act="src" data-op="retry" data-id="${esc(r.id)}">Retry</button>
        <button type="button" class="btn sm danger" data-act="src" data-op="cancel" data-id="${esc(r.id)}">Cancel</button></span>` : muted()),
    },
  ], res.data || [], { empty: 'Sourcing queue is empty' }) : errorCard(res.error, 'Sourcing queue unavailable');

  const parseForm = `<form class="card" data-form="parse">
    <div class="note" style="margin-bottom:8px">Paste a supplier WhatsApp message, email or price list. Claude extracts offers, matches SKUs and runs the compliance gate (licence, GST, batch, expiry, price ceiling).</div>
    <textarea name="text" rows="6" required minlength="3" placeholder="e.g. Dolo 650 15s - 28.50 net, 200 strips, batch DL2291 exp 08/2027, delivery tomorrow"></textarea>
    <div class="toolbar" style="margin:8px 0 0"><label>Supplier ID (optional)<input type="text" name="supplier_id" placeholder="e.g. SUP001"></label>
      <span class="spacer"></span><button type="submit" class="btn primary">Parse message</button></div>
  </form><div id="parse-result">${S.parse ? parseResultHtml(S.parse) : ''}</div>`;
  return pageHead('Sourcing', 'Bounce recovery: external sourcing requests and supplier offers')
    + section('Sourcing queue', filter + queue, { count: res.ok ? `${(res.data || []).length} requests` : '' })
    + section('Parse supplier message', parseForm);
}
function bestOffer(o) {
  if (!o) return muted('No offer');
  const fails = Array.isArray(o.gate_failures) && o.gate_failures.length ? ` <span class="muted" title="${esc(o.gate_failures.join(', '))}">${esc(truncate(o.gate_failures.join(', '), 40))}</span>` : '';
  return `${esc(o.supplier_name || o.supplier_id || '')} · ${esc(rupee(o.landed_cost))} ${statusChip(o.gate_status)}${fails}`;
}
function parseResultHtml(p) {
  if (p.error) {
    const unavailable = p.status === 503;
    return `<div class="panel ${unavailable ? 'warn' : 'bad'}"><div class="panel-head">${unavailable ? 'Claude unavailable' : 'Parse failed'}${p.status ? ` <span class="muted">HTTP ${esc(p.status)}</span>` : ''}</div><div>${esc(p.error)}</div></div>`;
  }
  const r = p.result || {};
  const stored = table([
    { key: 'sku_id', label: 'SKU', render: (x) => skuCell(x.sku_id, x.sku_name) },
    { key: 'product_name', label: 'As written', render: (x) => esc(x.product_name || '') },
    { key: 'price', label: 'Price', num: true, render: (x) => esc(rupee(x.price)) },
    { key: 'scheme', label: 'Scheme', render: (x) => (x.scheme ? esc(x.scheme) : muted()) },
    { key: 'available_qty', label: 'Qty', num: true, render: (x) => esc(qty(x.available_qty)) },
    { key: 'batch', label: 'Batch', render: (x) => (x.batch ? `<code>${esc(x.batch)}</code>` : muted()) },
    { key: 'expiry', label: 'Expiry', render: (x) => (x.expiry ? esc(x.expiry) : muted()) },
    { key: 'match_score', label: 'Match', num: true, render: (x) => esc(pct(x.match_score)) },
    { key: 'gate_status', label: 'Gate', render: (x) => statusChip(x.gate_status) },
    { key: 'gate_failures', label: 'Gate failures', render: (x) => chipList(x.gate_failures, 'bad') },
  ], r.offers_stored || [], { empty: 'No offers stored', cls: 'short' });
  const unmatched = table([
    { key: 'product_name', label: 'As written', render: (x) => esc(x.product_name || '') },
    { key: 'price', label: 'Price', num: true, render: (x) => esc(rupee(x.price)) },
    { key: 'match_score', label: 'Best match', num: true, render: (x) => esc(pct(x.match_score)) },
    { key: 'reason', label: 'Reason', render: (x) => chip(x.reason, 'warn') },
  ], r.unmatched || [], { empty: 'Everything matched', cls: 'short' });
  return `<div class="panel"><div class="panel-head">Parsed · supplier ${esc(r.supplier_id || 'unknown')} · ${esc(r.channel || '')}
      ${(r.sourcing_requests_resolved || []).length ? chip(`${r.sourcing_requests_resolved.length} sourcing requests resolved`, 'ok') : ''}</div>
    ${section('Offers stored', stored, { count: (r.offers_stored || []).length })}
    ${section('Unmatched', unmatched, { count: (r.unmatched || []).length })}</div>`;
}

/* ----- Ask AI ----- */
async function viewAsk() {
  const st = await settle(api('/agents/status'));
  state.llm = st.ok ? st.data : null;
  let note = '';
  if (!st.ok) note = `<div class="note-box">Agent status unavailable: ${esc(st.error.message)}</div>`;
  else if (!st.data.llm_available) {
    note = '<div class="note-box">Claude is not configured. Agent Q&amp;A needs <code>ANTHROPIC_API_KEY</code> on the server; morning briefings and decision explanations fall back to deterministic templates.</div>';
  }
  const status = st.ok ? `${st.data.llm_available ? chip('Claude available', 'ok') : chip('Claude unavailable', 'warn')} <span class="muted">${esc(st.data.model || '')}</span>` : '';
  const chat = `<div class="chat">
    <div class="suggest" aria-label="Suggested questions">${SUGGESTED_QUESTIONS.map((s) => `<button type="button" data-act="ask-suggest" data-q="${esc(s)}">${esc(s)}</button>`).join('')}</div>
    <div class="chat-log" id="chat-log">${chatLogHtml()}</div>
    <form class="chat-form" data-form="ask">
      <textarea name="question" rows="2" required minlength="2" placeholder="Ask about stock, bounces, POs, margin… (English or తెలుగు)" aria-label="Question"></textarea>
      <button type="submit" class="btn primary" id="ask-btn"${state.chatBusy ? ' disabled' : ''}>Ask</button>
    </form></div>`;
  const brief = `<div class="card"><div class="btn-row"><button type="button" class="btn" data-act="briefing">Generate morning briefing</button>
      <span class="note muted">Read-only: does not run the cycle or send notifications.</span></div>
    <div id="briefing">${state.briefing ? briefingHtml(state.briefing) : ''}</div></div>`;
  return pageHead('Ask AI', `Questions are answered over run date ${state.date}`) + note
    + section('Assistant', chat, { extra: status })
    + section('Morning briefing', brief);
}
function chatLogHtml() {
  if (!state.chat.length) return '<div class="muted">Ask a question, or pick one of the suggestions above.</div>';
  return state.chat.map((m) => {
    if (m.role === 'user') return `<div class="msg user">${esc(m.text)}</div>`;
    if (m.role === 'pending') return `<div class="msg ai"><span class="loading" style="padding:0"><span class="spinner"></span>Thinking…</span></div>`;
    if (m.role === 'error') return `<div class="msg err">${esc(m.text)}</div>`;
    return `<div class="msg ai"><p class="pre">${esc(m.text)}</p>${toolCallsHtml(m.tool_calls)}</div>`;
  }).join('');
}
function toolCallsHtml(calls) {
  if (!Array.isArray(calls) || !calls.length) return '';
  return `<details><summary>${calls.length} tool call${calls.length > 1 ? 's' : ''}</summary>${calls.map((c) => `<div><code>${esc(c.tool)}</code>${c.is_error ? ` ${chip('error', 'bad')}` : ''}<pre>${esc(JSON.stringify(c.input, null, 1))}</pre></div>`).join('')}</details>`;
}
function briefingHtml(b) {
  if (b.error) return `<div class="panel bad"><div class="panel-head">Briefing failed${b.status ? ` <span class="muted">HTTP ${esc(b.status)}</span>` : ''}</div><div>${esc(b.error)}</div></div>`;
  const src = b.source === 'claude' ? chip('Claude', 'info') : chip(b.source || 'runbook', 'warn');
  return `<div class="panel${b.source === 'claude' ? '' : ' warn'}"><div class="panel-head">Briefing · ${esc(b.run_date || '')} ${src}</div>
    <p class="pre">${esc(b.briefing)}</p>${toolCallsHtml(b.tool_calls)}</div>`;
}
function refreshChat() {
  const log = document.getElementById('chat-log');
  if (log) { log.innerHTML = chatLogHtml(); log.scrollTop = log.scrollHeight; }
  const btn = document.getElementById('ask-btn');
  if (btn) btn.disabled = state.chatBusy;
}

/* ----- Admin ----- */
async function viewAdmin() {
  const [jobs, dq, notes, erp] = await Promise.all([
    settle(api('/admin/jobs', { params: { date: state.date } })),
    settle(api('/admin/data-quality', { params: { date: state.date } })),
    settle(api('/admin/notifications', { params: { limit: 100 } })),
    settle(api('/admin/erp/health')),
  ]);
  const mode = currentMode();
  const m = state.meta || {};
  const controls = `<div class="grid-2">
    <div class="card"><div class="section-title">Decision cycle</div>
      <div class="note" style="margin-bottom:8px">Runs sync → data quality → signal → classification → forecast → bounce-to-stock → replenishment → pricing → orchestrator for <strong>${esc(state.date)}</strong>.</div>
      <button type="button" class="btn primary" data-act="run-cycle">Run decision cycle</button>
      <div id="cycle-result">${state.cycle ? cycleHtml(state.cycle) : ''}</div></div>
    <div class="card"><div class="section-title">Autonomy mode</div>
      <div class="seg" role="group" aria-label="Autonomy mode">${['SHADOW', 'ASSIST', 'AUTO'].map((x) => `<button type="button" data-act="autonomy" data-mode="${x}" aria-pressed="${mode === x}">${x}</button>`).join('')}</div>
      <div class="note muted" style="margin-top:8px">SHADOW: compute only, nothing written to the ERP. ASSIST: humans approve drafts. AUTO: low-risk drafts auto-approved. Runtime override; persist in config/policy.yaml.</div>
      <div class="kv" style="margin-top:12px">
        <div><span class="k">Policy version</span><span class="v">${esc(m.policy_version || DASH)}</span></div>
        <div><span class="k">Connector</span><span class="v">${esc(m.connector || DASH)}</span></div>
        <div><span class="k">ERP health</span><span class="v">${erp.ok ? `${statusChip(erp.data.ok ? 'OK' : 'FAILED')} ${esc(erp.data.connector || '')}${erp.data.error ? ` <span class="muted">${esc(erp.data.error)}</span>` : ''}` : `${chip('unreachable', 'bad')} <span class="muted">${esc(erp.error.message)}</span>`}</span></div>
        <div><span class="k">Latest run date</span><span class="v">${esc(m.latest_run_date || DASH)}</span></div>
        <div><span class="k">Business today / plan date</span><span class="v">${esc(m.business_today || DASH)} / ${esc(m.plan_date || DASH)}</span></div>
      </div></div></div>`;
  const dqTable = dq.ok ? table([
    { key: 'check', label: 'Check', render: (r) => `<code>${esc(r.check)}</code>` },
    { key: 'passed', label: 'Result', render: (r) => (r.passed ? chip('PASS', 'ok') : chip('FAIL', 'bad')) },
    { key: 'severity', label: 'Severity', render: (r) => chip(r.severity, r.severity === 'ERROR' ? 'bad' : 'warn') },
    { key: 'detail', label: 'Detail', cls: 'wrap', render: (r) => esc(r.detail || '') },
  ], dq.data, { empty: 'No data-quality results for this date', cls: 'short' }) : errorCard(dq.error);
  const notesTable = notes.ok ? table([
    { key: 'created_at', label: 'Time', render: (r) => esc(ts(r.created_at)) },
    { key: 'channel', label: 'Channel', render: (r) => chip(r.channel, 'neutral') },
    { key: 'recipient', label: 'Recipient' },
    { key: 'template', label: 'Template', render: (r) => `<code>${esc(r.template)}</code>` },
    { key: 'status', label: 'Status', render: (r) => statusChip(r.status) },
    { key: 'ref', label: 'Ref' },
    { key: 'body', label: 'Body', cls: 'wrap', render: (r) => `<span title="${esc(r.body)}">${esc(truncate(r.body, 160))}</span>` },
  ], notes.data, { empty: 'No notifications sent yet', cls: 'short' }) : errorCard(notes.error);
  return pageHead('Admin', 'Pipeline control, autonomy and platform health') + controls
    + section('Jobs', jobs.ok ? jobsTable(jobs.data) : errorCard(jobs.error), { count: jobs.ok ? jobs.data.length : '' })
    + section('Data quality', dqTable, { count: dq.ok ? `${dq.data.filter((x) => x.passed).length}/${dq.data.length} passed` : '' })
    + section('Notifications log', notesTable, { count: notes.ok ? notes.data.length : '' });
}
function cycleHtml(c) {
  if (c.error) return `<div class="panel bad"><div class="panel-head">Cycle failed${c.status ? ` <span class="muted">HTTP ${esc(c.status)}</span>` : ''}</div><div>${esc(c.error)}</div></div>`;
  return `<div class="panel"><div class="panel-head">Stage summary · ${esc(c.date)}</div>${tree(c.result)}</div>`;
}

const VIEWS = {
  overview: viewOverview,
  inventory: viewInventory,
  bounce: viewBounce,
  purchase: viewPurchase,
  margin: viewMargin,
  decisions: viewDecisions,
  sourcing: viewSourcing,
  ask: viewAsk,
  admin: viewAdmin,
};

async function render() {
  const seq = ++state.renderSeq;
  const view = document.getElementById('view');
  const keepScroll = render.keepScroll;
  render.keepScroll = false;
  const scrollY = window.scrollY;
  if (!keepScroll) view.innerHTML = loadingBlock();
  try {
    const html = await VIEWS[state.tab]();
    if (seq !== state.renderSeq) return;
    view.innerHTML = html;
    if (keepScroll) window.scrollTo(0, scrollY);
    if (state.tab === 'ask') refreshChat();
  } catch (err) {
    if (seq !== state.renderSeq) return;
    view.innerHTML = pageHead(TABS.find((t) => t.id === state.tab).label) + errorCard(err);
    toastError(err);
  }
}
/** Re-render the current tab after a mutation, keeping the scroll position. */
function rerender() { render.keepScroll = true; return render(); }

/* ========================================================= 8. SKU 360 drawer */
const SKU_SECTIONS = [
  ['decision', 'Decision'],
  ['stock_decision', 'Stock decision'],
  ['suggested_po', 'Suggested PO'],
  ['forecast', 'Forecast'],
  ['buffer', 'Buffer policy'],
  ['bounce_profile', 'Bounce profile'],
  ['class_history', 'Class history'],
  ['offers', 'Price offers'],
  ['batches', 'Batches'],
  ['sourcing', 'Sourcing'],
];
const SKU_OMIT = ['id', 'sku_id', 'inputs_hash', 'inputs_snapshot', 'name'];

async function openSku(skuId, wh) {
  const drawer = document.getElementById('drawer');
  const body = document.getElementById('drawer-body');
  document.getElementById('drawer-title').textContent = skuId;
  document.getElementById('drawer-sub').textContent = `${wh || 'All warehouses'} · ${state.date}`;
  body.innerHTML = loadingBlock('Loading SKU 360…');
  drawer.hidden = false;
  document.getElementById('drawer-scrim').hidden = false;
  drawer.dataset.current = skuId;
  drawer.querySelector('.close').focus();
  try {
    const d = await api(`/skus/${encodeURIComponent(skuId)}`, { params: { warehouse_id: wh, date: state.date } });
    if (drawer.dataset.current !== skuId) return;
    const s = d.sku || {};
    document.getElementById('drawer-title').textContent = `${s.name || skuId}`;
    document.getElementById('drawer-sub').textContent = `${skuId} · ${s.composition || ''} · ${wh || 'All warehouses'} · ${state.date}`;
    const master = kvGrid({
      manufacturer: s.manufacturer, pack: s.pack, pack_size: s.pack_size, schedule: s.schedule,
      mrp: rupee(s.mrp), ptr: rupee(s.ptr), cost: rupee(s.cost), normal_discount: pctv(s.normal_discount_pct),
      margin_floor: pctv(s.margin_floor_pct), price_ceiling: s.price_ceiling != null ? rupee(s.price_ceiling) : null,
      shelf_life_days: s.shelf_life_days, moq: s.moq, active: s.active,
    });
    const dec = (d.decision || []).map((x) => `<div class="card">
        <div class="btn-row">${prioChip(x.priority)} ${actionChip(x.action)} ${statusChip(x.status)} <span class="muted">${esc(x.warehouse_id)} · ${esc(x.sku_class || '')}</span></div>
        <p style="margin:8px 0">${esc(x.reason_text)}</p>${kvGrid(x.inputs_snapshot, { format: SNAPSHOT_FORMAT })}</div>`).join('') || '<div class="empty">No decision for this date</div>';
    const sections = SKU_SECTIONS.map(([k, label]) => {
      const rows = d[k] || [];
      const content = k === 'decision' ? dec : autoTable(rows, {
        omit: k === 'sourcing' ? [...SKU_OMIT, 'best_offer'] : SKU_OMIT,
        empty: `No ${label.toLowerCase()} data`,
        render: { status: (v) => statusChip(v), gate_status: (v) => statusChip(v), action: (v) => actionChip(v), priority: (v) => (/^P\d$/.test(String(v)) ? prioChip(v) : poPrioChip(v)) },
      });
      return section(label, content, { count: rows.length });
    }).join('');
    body.innerHTML = section('SKU master', `<div class="card">${master}</div>`) + sections;
  } catch (err) {
    if (drawer.dataset.current !== skuId) return;
    body.innerHTML = errorCard(err, 'Could not load SKU 360');
    toastError(err);
  }
}
function closeDrawer() {
  document.getElementById('drawer').hidden = true;
  document.getElementById('drawer-scrim').hidden = true;
  document.getElementById('drawer').dataset.current = '';
}

/* ============================================================== 9. actions */
const ACTION_HANDLERS = {
  'close-drawer': () => closeDrawer(),

  async 'po-approve'(btn) {
    const id = btn.dataset.id;
    const row = state.po.rows.find((r) => r.po_draft_id === id);
    const edited = state.po.edits[id];
    const body = edited !== undefined && row && Number(edited) !== row.qty ? { qty: Number(edited) } : {};
    setBusy(btn, true);
    try {
      const res = await api(`/po-drafts/${encodeURIComponent(id)}/approve`, { method: 'POST', body });
      delete state.po.edits[id];
      state.po.selected.delete(id);
      toast(`${id} ${res.status}${res.erp_ref ? ` · ERP ${res.erp_ref}` : ''}${res.note ? ` · ${res.note}` : ''}`, 'ok', 'PO approved');
      await rerender();
    } catch (err) { toastError(err, 'Approve failed'); setBusy(btn, false); }
  },

  async 'po-reject'(btn) {
    const id = btn.dataset.id;
    const reason = await askReason('Reject purchase order', id);
    if (!reason) return;
    setBusy(btn, true);
    try {
      await api(`/po-drafts/${encodeURIComponent(id)}/reject`, { method: 'POST', body: { reason } });
      state.po.selected.delete(id);
      toast(`${id} rejected`, 'ok');
      await rerender();
    } catch (err) { toastError(err, 'Reject failed'); setBusy(btn, false); }
  },

  async 'po-bulk'(btn) {
    const ids = [...state.po.selected];
    if (!ids.length) return;
    const edited = ids.filter((id) => state.po.edits[id] !== undefined);
    if (edited.length) {
      const ok = await modalForm({ title: 'Approve selected', text: `${edited.length} selected line(s) have edited quantities. Bulk approval uses the suggested quantities; approve edited rows individually to keep your edits.`, submit: `Approve ${ids.length} at suggested qty` });
      if (!ok) return;
    }
    setBusy(btn, true);
    try {
      const res = await api('/po-drafts/approve-bulk', { method: 'POST', body: { po_draft_ids: ids } });
      const results = res.results || [];
      const failed = results.filter((r) => r.error);
      state.po.selected.clear();
      if (failed.length) toast(`${results.length - failed.length} approved, ${failed.length} failed: ${failed.slice(0, 3).map((f) => `${f.po_draft_id}: ${f.error}`).join('; ')}`, 'error', 'Bulk approval');
      else toast(`${results.length} PO lines approved`, 'ok', 'Bulk approval');
      await rerender();
    } catch (err) { toastError(err, 'Bulk approval failed'); setBusy(btn, false); }
  },

  async 'offer-approve'(btn) {
    const id = btn.dataset.id;
    setBusy(btn, true);
    try {
      const res = await api(`/price-offers/${encodeURIComponent(id)}/approve`, { method: 'POST' });
      toast(`${id} ${res.status}${res.erp_ref ? ` · ERP ${res.erp_ref}` : ''}`, 'ok', 'Offer approved');
      await rerender();
    } catch (err) { toastError(err, 'Approve failed'); setBusy(btn, false); }
  },

  async 'offer-reject'(btn) {
    const id = btn.dataset.id;
    const reason = await askReason('Reject offer', id);
    if (!reason) return;
    setBusy(btn, true);
    try {
      await api(`/price-offers/${encodeURIComponent(id)}/reject`, { method: 'POST', body: { reason } });
      toast(`${id} rejected`, 'ok');
      await rerender();
    } catch (err) { toastError(err, 'Reject failed'); setBusy(btn, false); }
  },

  async explain(btn) {
    const id = Number(btn.dataset.id);
    const lang = btn.dataset.lang || 'en';
    const panel = document.querySelector(`[data-explain="${id}"]`);
    if (panel) panel.innerHTML = loadingBlock(lang === 'te' ? 'వివరణ సిద్ధం చేస్తోంది…' : 'Explaining…');
    setBusy(btn, true);
    try {
      const res = await api(`/agents/explain/${id}`, { params: { lang } });
      state.dec.explain[id] = res;
    } catch (err) {
      state.dec.explain[id] = { error: errText(err) };
      toastError(err, 'Explain failed');
    }
    setBusy(btn, false);
    const p = document.querySelector(`[data-explain="${id}"]`);
    if (p) p.innerHTML = explainHtml(state.dec.explain[id]);
  },

  async override(btn) {
    const id = Number(btn.dataset.id);
    const d = state.dec.items[id];
    const v = await modalForm({
      title: 'Override decision',
      text: d ? `${d.sku_id} · ${d.warehouse_id} · currently ${d.action}` : `Decision ${id}`,
      submit: 'Override',
      fields: [
        { name: 'action', label: 'New action', type: 'select', value: d && d.action, options: ACTIONS.map((a, i) => ({ value: a, label: `P${i} · ${a}` })) },
        { name: 'reason', label: 'Reason (min 3 characters)', type: 'textarea', required: true, minlength: 3 },
      ],
    });
    if (!v) return;
    try {
      const res = await api(`/decisions/${id}/override`, { method: 'POST', body: { action: v.action, reason: v.reason } });
      toast(`Decision ${id} → ${res.override_action}`, 'ok', 'Override recorded');
      await rerender();
    } catch (err) { toastError(err, 'Override failed'); }
  },

  async src(btn) {
    const { id, op } = btn.dataset;
    setBusy(btn, true);
    try {
      const res = await api(`/sourcing/requests/${encodeURIComponent(id)}/${op}`, { method: 'POST' });
      toast(`${id}: ${res.status}${res.failure_reason ? ` (${res.failure_reason})` : ''}`, 'ok', humanize(op));
      await rerender();
    } catch (err) { toastError(err, `${humanize(op)} failed`); setBusy(btn, false); }
  },

  'ask-suggest'(btn) { sendQuestion(btn.dataset.q); },

  async briefing(btn) {
    setBusy(btn, true);
    const el = document.getElementById('briefing');
    if (el) el.innerHTML = loadingBlock('Writing briefing…');
    try {
      state.briefing = await api('/agents/ops/run', { method: 'POST', body: { run_cycle: false, notify: false } });
    } catch (err) {
      state.briefing = { error: err.message, status: err.status };
      toastError(err, 'Briefing failed');
    }
    setBusy(btn, false);
    const e2 = document.getElementById('briefing');
    if (e2) e2.innerHTML = briefingHtml(state.briefing);
  },

  async 'run-cycle'(btn) {
    const ok = await modalForm({
      title: 'Run decision cycle',
      text: `Run the full cycle for ${state.date} with ERP sync. This can take a while and replaces the day's engine outputs (human approvals and overrides are preserved).`,
      submit: 'Run cycle',
      fields: [{ name: 'background', label: 'Execution', type: 'select', value: 'wait', options: [{ value: 'wait', label: 'Wait for the stage summary' }, { value: 'bg', label: 'Run in background (check Jobs afterwards)' }] }],
    });
    if (!ok) return;
    const background = ok.background === 'bg';
    setBusy(btn, true);
    const el = document.getElementById('cycle-result');
    if (el) el.innerHTML = loadingBlock('Running cycle…');
    try {
      const result = await api('/admin/run-cycle', { method: 'POST', body: { date: state.date, sync: true, background } });
      state.cycle = { date: state.date, result };
      toast(background ? `Cycle accepted for ${state.date}; refresh Jobs to follow progress` : `Cycle finished for ${state.date}`, 'ok');
      await loadMeta(false);
    } catch (err) {
      state.cycle = { error: err.message, status: err.status };
      toastError(err, 'Cycle failed');
    }
    setBusy(btn, false);
    const e2 = document.getElementById('cycle-result');
    if (e2) e2.innerHTML = cycleHtml(state.cycle);
  },

  async autonomy(btn) {
    const mode = btn.dataset.mode;
    if (mode === currentMode()) return;
    setBusy(btn, true);
    try {
      const res = await api('/admin/policy/autonomy', { method: 'PUT', body: { mode } });
      if (state.meta) state.meta.autonomy_mode = res.mode;
      updateHeader();
      toast(`${res.mode}${res.note ? ` · ${res.note}` : ''}`, 'ok', 'Autonomy mode changed');
      await rerender();
    } catch (err) { toastError(err, 'Mode change failed'); setBusy(btn, false); }
  },
};

async function sendQuestion(text) {
  const question = String(text || '').trim();
  if (question.length < 2 || state.chatBusy) return;
  state.chat.push({ role: 'user', text: question });
  state.chat.push({ role: 'pending' });
  state.chatBusy = true;
  refreshChat();
  try {
    const res = await api('/agents/ask', { method: 'POST', body: { question, date: state.date || undefined } });
    state.chat.pop();
    state.chat.push({ role: 'ai', text: res.answer || '(empty answer)', tool_calls: res.tool_calls });
  } catch (err) {
    state.chat.pop();
    const hint = err.status === 503 ? '' : (err.status >= 500 ? ' The agent may need ANTHROPIC_API_KEY on the server.' : '');
    state.chat.push({ role: 'error', text: `${errText(err)}.${hint}` });
  }
  state.chatBusy = false;
  refreshChat();
}

function onClick(e) {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const sku = t.closest('[data-sku]');
  if (sku) { e.preventDefault(); openSku(sku.dataset.sku, sku.dataset.wh || state.wh); return; }
  const act = t.closest('[data-act]');
  if (act && ACTION_HANDLERS[act.dataset.act]) { e.preventDefault(); ACTION_HANDLERS[act.dataset.act](act); return; }
  const fchip = t.closest('[data-dec-action]');
  if (fchip) { state.dec.action = fchip.dataset.decAction; state.dec.open.clear(); render(); return; }
  if (t.id === 'drawer-scrim') { closeDrawer(); return; }
  const row = t.closest('[data-dec-row]');
  if (row && !t.closest('button, a, input, select, textarea')) toggleDecision(row);
}
function toggleDecision(row) {
  const id = Number(row.dataset.decRow);
  const D = state.dec;
  const detail = document.querySelector(`[data-dec-detail="${id}"]`);
  const open = !D.open.has(id);
  if (open) D.open.add(id); else D.open.delete(id);
  row.classList.toggle('is-open', open);
  row.setAttribute('aria-expanded', String(open));
  if (detail) {
    if (open && D.items[id]) detail.firstElementChild.innerHTML = decisionDetail(D.items[id]);
    detail.hidden = !open;
  }
}
function onKeydown(e) {
  if (e.key === 'Escape' && !document.getElementById('drawer').hidden && !document.querySelector('#modal-root .modal')) closeDrawer();
  if ((e.key === 'Enter' || e.key === ' ') && e.target instanceof Element && e.target.matches('[data-dec-row]')) {
    e.preventDefault();
    toggleDecision(e.target);
  }
  if (e.key === 'Enter' && !e.shiftKey && e.target instanceof Element && e.target.matches('[data-form="ask"] textarea')) {
    e.preventDefault();
    e.target.form.requestSubmit();
  }
}
function onInput(e) {
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (t.matches('[data-po-qty]')) {
    const id = t.dataset.poQty;
    const row = state.po.rows.find((r) => r.po_draft_id === id);
    const v = t.value === '' ? null : Number(t.value);
    if (v === null || !Number.isFinite(v) || v < 0 || (row && v === row.qty)) delete state.po.edits[id];
    else state.po.edits[id] = v;
    t.classList.toggle('edited', state.po.edits[id] !== undefined);
    const cellEl = document.querySelector(`[data-po-val="${CSS.escape(id)}"]`);
    if (cellEl && row) cellEl.textContent = inr((toNum(poQty(row)) || 0) * (row.unit_cost || 0));
  }
}
function onChange(e) {
  const t = e.target;
  if (!(t instanceof Element)) return;
  if (t.matches('[data-po-filter]')) { state.po[t.dataset.poFilter] = t.value; refreshPoTable(); return; }
  if (t.matches('[data-po-sel]')) {
    if (t.checked) state.po.selected.add(t.dataset.poSel); else state.po.selected.delete(t.dataset.poSel);
    updateBulkButton();
    return;
  }
  if (t.matches('[data-po-selall]')) {
    for (const r of poFiltered()) {
      if (!OPEN_PO_STATES.includes(r.status)) continue;
      if (t.checked) state.po.selected.add(r.po_draft_id); else state.po.selected.delete(r.po_draft_id);
    }
    refreshPoTable();
    return;
  }
  if (t.matches('[data-src-filter]')) { state.sourcing.status = t.value; render(); }
}
async function onSubmit(e) {
  const form = e.target;
  if (!(form instanceof HTMLFormElement) || !form.dataset.form) return;
  e.preventDefault();
  if (form.dataset.form === 'ask') {
    const ta = form.elements.question;
    const text = ta.value;
    ta.value = '';
    sendQuestion(text);
  } else if (form.dataset.form === 'parse') {
    const btn = form.querySelector('button[type="submit"]');
    const text = form.elements.text.value.trim();
    const supplierId = form.elements.supplier_id.value.trim();
    if (text.length < 3) return;
    setBusy(btn, true);
    const out = document.getElementById('parse-result');
    if (out) out.innerHTML = loadingBlock('Parsing with Claude…');
    try {
      const result = await api('/supplier-offers/parse', { method: 'POST', body: { text, supplier_id: supplierId || null } });
      state.sourcing.parse = { result };
      toast(`${(result.offers_stored || []).length} offers stored, ${(result.unmatched || []).length} unmatched`, 'ok', 'Message parsed');
    } catch (err) {
      state.sourcing.parse = { error: err.message, status: err.status };
      toastError(err, err.status === 503 ? 'Claude unavailable' : 'Parse failed');
    }
    setBusy(btn, false);
    const o2 = document.getElementById('parse-result');
    if (o2) o2.innerHTML = parseResultHtml(state.sourcing.parse);
  }
}

/* ============================================ 10. header, routing, bootstrap */
function currentMode() { return String((state.meta && state.meta.autonomy_mode) || '').toUpperCase(); }
function isShadow() { return currentMode() === 'SHADOW'; }

function updateHeader() {
  const mode = currentMode() || 'UNKNOWN';
  const badge = document.getElementById('mode-badge');
  badge.textContent = mode;
  badge.className = `mode-badge mode-${mode}`;
  badge.title = `Autonomy mode: ${mode}`;
  document.getElementById('shadow-banner').hidden = mode !== 'SHADOW';
  const m = state.meta;
  if (m) document.getElementById('brand-sub').textContent = `Predictive Distribution Intelligence · ${m.policy_version || ''} · ${m.connector || ''} connector`;
}

function renderTabs() {
  document.getElementById('tabs').innerHTML = TABS.map((t) => `<a href="#${t.id}"${t.id === state.tab ? ' aria-current="page"' : ''}>${esc(t.label)}</a>`).join('');
  const cur = document.querySelector('#tabs [aria-current="page"]');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function route() {
  const id = window.location.hash.replace(/^#\/?/, '');
  state.tab = TABS.some((t) => t.id === id) ? id : 'overview';
  renderTabs();
  render();
}

async function loadMeta(setDate = true) {
  try {
    state.meta = await api('/meta');
    if (setDate && !state.date) state.date = state.meta.latest_run_date || state.meta.business_today || '';
  } catch (err) {
    toastError(err, 'Could not load platform metadata');
    if (!state.date) state.date = new Date().toISOString().slice(0, 10);
  }
  document.getElementById('date-input').value = state.date;
  updateHeader();
}

async function init() {
  state.key = store.get('si.apiKey') || '';
  state.wh = store.get('si.warehouse') || '';
  const whSel = document.getElementById('wh-select');
  if (![...whSel.options].some((o) => o.value === state.wh)) state.wh = '';
  whSel.value = state.wh;
  const keyInput = document.getElementById('key-input');
  keyInput.value = state.key;

  whSel.addEventListener('change', () => { state.wh = whSel.value; store.set('si.warehouse', state.wh); state.po.selected.clear(); render(); });
  document.getElementById('date-input').addEventListener('change', (e) => {
    state.date = e.target.value || (state.meta && state.meta.latest_run_date) || '';
    e.target.value = state.date;
    state.po.selected.clear();
    state.po.edits = {};
    state.dec.open.clear();
    render();
  });
  keyInput.addEventListener('change', async () => {
    state.key = keyInput.value.trim();
    store.set('si.apiKey', state.key);
    toast(state.key ? 'API key saved in this browser' : 'API key cleared (dev mode)', 'info');
    await loadMeta(false);
    render();
  });

  document.addEventListener('click', onClick);
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('input', onInput);
  document.addEventListener('change', onChange);
  document.addEventListener('submit', onSubmit);
  window.addEventListener('hashchange', route);

  await loadMeta(true);
  route();
}

document.addEventListener('DOMContentLoaded', init);
