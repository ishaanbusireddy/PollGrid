/* PollsWindow.js — the global polls browser (#/polls): every poll ever
   ingested, filterable by race type, state, pollster, sampled population,
   date range, and methodology text; paged. Every row carries pollster +
   accuracy-grade chip, field dates, n, MoE, toplines, the house-adjusted
   numbers actually used in the average, and a link out to the pollster's own
   release (primary-source rule — never a paraphrase).

   Also exports pollsTable(), the same list embedded pre-filtered on race /
   state / county pages. County-scope honesty rule: county pages label
   state/district polls as "covering" the county — a poll belongs to the race
   it was fielded for, not to a click. */

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function gradeChip(grade) {
  return grade ? `<span class="grade-chip" title="pollster accuracy grade">${escapeHtml(grade)}</span>` : '<span class="grade-chip dim" title="no grade yet">–</span>';
}

function fmtResults(obj) {
  if (!obj) return '<span class="dim">—</span>';
  return Object.entries(obj)
    .map(([p, v]) => `<span class="chip ${p === 'DEM' ? 'dem' : p === 'REP' ? 'rep' : 'other'}">${escapeHtml(p)} ${Number(v).toFixed(1)}</span>`)
    .join(' ');
}

/**
 * Render a polls table from contract rows.
 * @param {Array} rows /api/polls rows
 * @param {{scopeNote?:string, compact?:boolean}} opts
 */
export function pollsTable(rows, opts = {}) {
  const wrap = document.createElement('div');
  if (opts.scopeNote) {
    const n = document.createElement('div');
    n.className = 'dim';
    n.style.cssText = 'font-size:11px;margin-bottom:6px';
    n.textContent = opts.scopeNote;
    wrap.appendChild(n);
  }
  if (!rows || !rows.length) {
    wrap.insertAdjacentHTML('beforeend',
      '<div class="empty">No polls found.<span class="why">/api/polls returned nothing for this scope</span></div>');
    return wrap;
  }
  const t = document.createElement('table');
  t.className = 'grid';
  t.innerHTML = `<thead><tr>
      <th>Pollster</th><th>Race</th><th>Field</th><th class="num">n</th><th class="num">MoE</th>
      <th>Toplines</th><th>House-adj.</th><th></th>
    </tr></thead>`;
  const tb = document.createElement('tbody');
  for (const p of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${gradeChip(p.pollster_grade)} ${escapeHtml(p.pollster)}
        ${p.is_synthetic ? '<span class="chip warn synth-chip" title="synthetic demo row — remove with scripts/purge_synthetic.py">SYNTH</span>' : ''}</td>
      <td>${p.race_id ? `<a href="#/race/${p.race_id}">${escapeHtml(p.race_name || 'race #' + p.race_id)}</a>` : escapeHtml(p.race_name || '—')}</td>
      <td class="mono" style="font-size:11px">${escapeHtml(p.field_start || '?')} → ${escapeHtml(p.field_end || '?')}<br>
        <span class="dim">${escapeHtml(p.population || '')}</span></td>
      <td class="num">${p.sample_size ?? '—'}</td>
      <td class="num">${p.moe != null ? '±' + p.moe : '—'}</td>
      <td>${fmtResults(p.results)}</td>
      <td>${fmtResults(p.adjusted)}</td>
      <td>${p.release_url ? `<a class="pollster-link" href="${escapeHtml(p.release_url)}" target="_blank" rel="noopener">release ↗</a>` : ''}</td>`;
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  wrap.appendChild(t);
  return wrap;
}

const PAGE = 50;

export class PollsWindow {
  /** @param {HTMLElement} el @param {{api:object, states:Array<{key,name}>}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.offset = 0;
    this.filters = {};
  }

  show() {
    this.el.innerHTML = `
      <h1>Polls</h1>
      <p class="dim">Every individual poll the platform has ingested — filter, page, and follow the link to the pollster's own release. Grades are transparent accuracy scores from /api/pollsters/ratings.</p>
      <fieldset><legend class="dim" style="font-size:11px;padding:0 6px">Filters</legend>
      <div class="row" style="padding:8px">
        <label class="f">Race type
          <select name="race_type">
            <option value="">any</option>
            <option>president</option><option>senate</option><option>governor</option><option>house</option>
          </select></label>
        <label class="f">State
          <select name="state"><option value="">any</option>
            ${this.bag.states.map((s) => `<option value="${s.key}">${escapeHtml(s.name)}</option>`).join('')}
          </select></label>
        <label class="f">Pollster<input name="pollster" size="14" placeholder="name…"></label>
        <label class="f">Population
          <select name="population"><option value="">any</option><option value="lv">LV</option><option value="rv">RV</option><option value="a">Adults</option></select></label>
        <label class="f">From<input type="date" name="from"></label>
        <label class="f">To<input type="date" name="to"></label>
        <label class="f">Methodology<input name="methodology" size="14" placeholder="e.g. live phone"></label>
        <button class="primary" data-act="apply">Apply</button>
      </div></fieldset>
      <div class="polls-results"></div>
      <div class="row mt">
        <button data-act="prev">← Prev</button>
        <span class="mono dim polls-page"></span>
        <button data-act="next">Next →</button>
      </div>`;
    this.el.querySelector('[data-act=apply]').addEventListener('click', () => { this.offset = 0; this._read(); this.load(); });
    this.el.querySelector('[data-act=prev]').addEventListener('click', () => { this.offset = Math.max(0, this.offset - PAGE); this.load(); });
    this.el.querySelector('[data-act=next]').addEventListener('click', () => {
      if (this.total == null || this.offset + PAGE < this.total) { this.offset += PAGE; this.load(); }
    });
    this._read();
    this.load();
  }

  _read() {
    const f = {};
    for (const input of this.el.querySelectorAll('fieldset [name]')) {
      if (input.value) f[input.name] = input.value;
    }
    this.filters = f;
  }

  async load() {
    const box = this.el.querySelector('.polls-results');
    if (!box) return;
    box.innerHTML = '<div class="dim">loading…</div>';
    const res = await this.bag.api.polls({ ...this.filters, limit: PAGE, offset: this.offset });
    if (!box.isConnected) return;
    box.innerHTML = '';
    if (!res) {
      box.innerHTML = '<div class="empty">Poll data unavailable.<span class="why">GET /api/polls failed — backend offline or route empty</span></div>';
      this.total = null;
    } else {
      this.total = res.total ?? (res.rows || []).length;
      let rows = res.rows || [];
      // methodology text filter is applied client-side if the row carries it
      if (this.filters.methodology) {
        const q = this.filters.methodology.toLowerCase();
        rows = rows.filter((r) => !r.methodology || String(r.methodology).toLowerCase().includes(q));
      }
      box.appendChild(pollsTable(rows));
    }
    const pageEl = this.el.querySelector('.polls-page');
    if (pageEl) {
      pageEl.textContent = this.total
        ? `${this.offset + 1}–${Math.min(this.offset + PAGE, this.total)} of ${this.total}`
        : '0 results';
    }
  }
}
