/* Scorecard.js — (#/scorecard) the public Brier scorecard from
   /api/forecast/scorecard, including the failing categories ("the honesty is
   the moat"), plus chamber-control cards (senate/house/ec control
   probability + seat-distribution histogram as inline SVG). */

import { histogram } from '../charts.js';
import { escapeHtml } from './PollsWindow.js';

const MAJORITY = { senate: 51, house: 218, ec: 270 };

export class Scorecard {
  /** @param {HTMLElement} el @param {{api:object}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
  }

  async show() {
    this.el.innerHTML = `<h1>Forecast scorecard</h1>
      <p class="dim">Nightly Brier backtest against the archive — a race-type only earns a visible forecast after clearing the ceiling over a minimum of graded predictions. Failing categories are shown, not hidden.</p>
      <div class="sc-table"><div class="dim">loading…</div></div>
      <h2 class="mt">Chamber control</h2>
      <div class="sc-chambers row" style="align-items:flex-start"></div>`;

    const [rows, senate, house, ec] = await Promise.all([
      this.bag.api.scorecard(),
      this.bag.api.chamber('senate'),
      this.bag.api.chamber('house'),
      this.bag.api.chamber('ec'),
    ]);
    if (!this.el.querySelector('.sc-table')) return;

    const tableBox = this.el.querySelector('.sc-table');
    if (!rows || !rows.length) {
      tableBox.innerHTML = '<div class="empty">Scorecard unavailable.<span class="why">GET /api/forecast/scorecard returned nothing — backend offline or no graded predictions yet</span></div>';
    } else {
      const t = document.createElement('table');
      t.className = 'grid';
      t.innerHTML = `<thead><tr><th>Category</th><th>Model</th><th class="num">Brier</th><th class="num">Graded</th><th>Gate</th><th>Live</th></tr></thead>`;
      const tb = document.createElement('tbody');
      for (const r of rows) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${escapeHtml(r.category)}</td>
          <td class="mono" style="font-size:11px">${escapeHtml(r.model || '—')}</td>
          <td class="num">${r.brier != null ? Number(r.brier).toFixed(3) : '—'}</td>
          <td class="num">${r.n_graded ?? 0}</td>
          <td>${r.passed ? '<span class="chip ok">passed</span>' : '<span class="chip err">failing</span>'}</td>
          <td>${r.live ? '<span class="chip accent">live</span>' : '<span class="chip">gated off</span>'}</td>`;
        tb.appendChild(tr);
      }
      t.appendChild(tb);
      tableBox.innerHTML = '';
      tableBox.appendChild(t);
    }

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
}
