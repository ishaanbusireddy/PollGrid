/* Scorecard.js — (#/scorecard) the public Brier scorecard from
   /api/forecast/scorecard, including the failing categories ("the honesty is
   the moat"), plus chamber-control cards (senate/house/ec control
   probability + seat-distribution histogram as inline SVG).

   v3.0: sortable columns (click a header), and a per-category Brier trend
   sparkline rebuilt client-side from /api/export/backtest_results — omitted
   gracefully when the export is empty or unavailable. */

import { histogram, sparkline } from '../charts.js';
import { escapeHtml } from './PollsWindow.js';
import { skeleton } from '../anim.js';

const MAJORITY = { senate: 51, house: 218, ec: 270 };

const COLUMNS = [
  { key: 'category', label: 'Category', num: false },
  { key: 'model', label: 'Model', num: false },
  { key: 'brier', label: 'Brier', num: true },
  { key: 'n_graded', label: 'Graded', num: true },
  { key: 'passed', label: 'Gate', num: true },
  { key: 'live', label: 'Live', num: true },
  { key: 'trend', label: 'Trend', num: false, nosort: true },
];

export class Scorecard {
  /** @param {HTMLElement} el @param {{api:object}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.rows = [];
    this.trends = {};   // category -> [brier by as_of asc]
    this.sortKey = 'category';
    this.sortDir = 1;
  }

  async show() {
    this.el.innerHTML = `<h1>Forecast scorecard</h1>
      <p class="dim">Nightly Brier backtest against the archive — a race-type only earns a visible forecast after clearing the ceiling over a minimum of graded predictions. Failing categories are shown, not hidden. Click a column header to sort.</p>
      <div class="sc-table"></div>
      <h2 class="mt">Chamber control</h2>
      <div class="sc-chambers row" style="align-items:flex-start"></div>`;
    this.el.querySelector('.sc-table').appendChild(skeleton(5, { table: true }));
    this.el.querySelector('.sc-chambers').appendChild(skeleton(4));

    const [rows, senate, house, ec, backtests] = await Promise.all([
      this.bag.api.scorecard(),
      this.bag.api.chamber('senate'),
      this.bag.api.chamber('house'),
      this.bag.api.chamber('ec'),
      this.bag.api.exportTable('backtest_results'),
    ]);
    if (!this.el.querySelector('.sc-table')) return;

    // Brier history per (category) — from the open-data export, client-filtered
    this.trends = {};
    if (Array.isArray(backtests) && backtests.length) {
      const byKey = {};
      for (const b of backtests) {
        if (b.category == null || b.brier == null) continue;
        (byKey[`${b.category}|${b.model || ''}`] ||= []).push(b);
      }
      for (const [k, arr] of Object.entries(byKey)) {
        arr.sort((a, b) => String(a.as_of).localeCompare(String(b.as_of)));
        this.trends[k] = arr.map((x) => Number(x.brier));
      }
    }

    this.rows = rows || [];
    this._renderTable();

    const chBox = this.el.querySelector('.sc-chambers');
    chBox.innerHTML = '';
    const cards = [['Senate', 'senate', senate], ['House', 'house', house], ['Electoral College', 'ec', ec]];
    for (const [label, key, data] of cards) {
      const card = document.createElement('div');
      card.className = 'panel';
      card.style.cssText = 'flex:1;min-width:280px;max-width:420px;margin:0';
      if (!data) {
        card.innerHTML = `<div class="panel-head">${label}</div>
          <div class="panel-body"><div class="empty">No simulation.<span class="why">GET /api/forecast/chamber/${key} unavailable</span></div></div>`;
      } else {
        const p = data.dem_control_prob;
        card.innerHTML = `<div class="panel-head">${label} · ${data.n_sims ?? '?'} sims · as of ${escapeHtml(data.as_of || '?')}</div>
          <div class="panel-body">
            <div class="kv"><span class="k">Dem control probability</span>
              <span class="v" style="color:var(--dem)">${p != null ? (p * 100).toFixed(1) + '%' : '—'}</span></div>
            <div class="kv"><span class="k">Rep control probability</span>
              <span class="v" style="color:var(--rep)">${p != null ? ((1 - p) * 100).toFixed(1) + '%' : '—'}</span></div>
            <div class="mt hist"></div>
            <div class="dim" style="font-size:10px">Dem-seat distribution; blue bars at or above the ${MAJORITY[key]}-seat majority line.</div>
          </div>`;
        card.querySelector('.hist').appendChild(histogram(data.seat_distribution || {}, { majority: MAJORITY[key] }));
      }
      chBox.appendChild(card);
    }
  }

  _trendFor(r) {
    return this.trends[`${r.category}|${r.model || ''}`]
      || this.trends[`${r.category}|`] || null;
  }

  _renderTable() {
    const tableBox = this.el.querySelector('.sc-table');
    if (!tableBox) return;
    if (!this.rows.length) {
      tableBox.innerHTML = '<div class="empty">Scorecard unavailable.<span class="why">GET /api/forecast/scorecard returned nothing — backend offline or no graded predictions yet</span></div>';
      return;
    }
    const hasTrends = this.rows.some((r) => (this._trendFor(r) || []).length > 1);
    const cols = COLUMNS.filter((c) => c.key !== 'trend' || hasTrends);

    const sorted = [...this.rows].sort((a, b) => {
      const c = cols.find((x) => x.key === this.sortKey) || cols[0];
      const av = a[c.key], bv = b[c.key];
      const cmp = c.num
        ? (Number(av ?? -Infinity) - Number(bv ?? -Infinity))
        : String(av ?? '').localeCompare(String(bv ?? ''));
      return cmp * this.sortDir;
    });

    const t = document.createElement('table');
    t.className = 'grid';
    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    for (const c of cols) {
      const th = document.createElement('th');
      th.textContent = c.label;
      if (c.num) th.classList.add('num');
      if (!c.nosort) {
        th.classList.add('sortable');
        if (this.sortKey === c.key) th.classList.add(this.sortDir > 0 ? 'sort-asc' : 'sort-desc');
        th.addEventListener('click', () => {
          if (this.sortKey === c.key) this.sortDir *= -1;
          else { this.sortKey = c.key; this.sortDir = c.num ? -1 : 1; }
          this._renderTable();
        });
      }
      htr.appendChild(th);
    }
    thead.appendChild(htr);
    t.appendChild(thead);

    const tb = document.createElement('tbody');
    for (const r of sorted) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(r.category)}</td>
        <td class="mono" style="font-size:11px">${escapeHtml(r.model || '—')}</td>
        <td class="num">${r.brier != null ? Number(r.brier).toFixed(3) : '—'}</td>
        <td class="num">${r.n_graded ?? 0}</td>
        <td>${r.passed ? '<span class="chip ok">passed</span>' : '<span class="chip err">failing</span>'}</td>
        <td>${r.live ? '<span class="chip accent">live</span>' : '<span class="chip">gated off</span>'}</td>`;
      if (hasTrends) {
        const td = document.createElement('td');
        const series = this._trendFor(r);
        if (series && series.length > 1) {
          td.title = `Brier over the last ${series.length} backtests (lower is better)`;
          td.appendChild(sparkline(series, { w: 90, h: 22 }));
        } else td.innerHTML = '<span class="dim" style="font-size:10px">—</span>';
        tr.appendChild(td);
      }
      tb.appendChild(tr);
    }
    t.appendChild(tb);
    tableBox.innerHTML = '';
    tableBox.appendChild(t);
    if (hasTrends) {
      tableBox.insertAdjacentHTML('beforeend',
        '<div class="dim" style="font-size:10px;margin-top:4px">trend sparklines rebuilt client-side from /api/export/backtest_results — the same open data anyone can pull</div>');
    }
  }
}
