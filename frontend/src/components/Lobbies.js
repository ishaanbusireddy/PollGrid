/* Lobbies.js — the Influence ledger directory (#/lobbies): every curated
   lobbying org / PAC with LDA-disclosed lobbying spend + FEC independent
   expenditures, filterable by sector, sortable by total spend or name. Each
   row opens the lobby detail pane (#/lobby/{id}). Amounts always carry their
   citation — the ledger displays only what a public filing backs. */

import { escapeHtml } from './PollsWindow.js';
import { icon } from '../icons.js';
import { skeleton } from '../anim.js';

function fmtMoney(v) {
  const n = Number(v) || 0;
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(0) + 'K';
  return '$' + n.toLocaleString();
}

export class Lobbies {
  /** @param {HTMLElement} el @param {{api:object, navigate:Function}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.sector = '';
    this.sort = 'spend';
    this.sectors = null; // learned from the unfiltered response
  }

  show() {
    this.el.innerHTML = `
      <h1>${icon('org')} Influence ledger</h1>
      <p class="dim">Lobbying organizations and PACs — LDA lobbying disclosures plus FEC independent
      expenditures, rolled up per organization. Every number traces to a public filing; endorsements
      display only with the org's own announcement URL.</p>
      <div class="lobby-toolbar">
        <label class="f">Sector
          <select name="sector"><option value="">all sectors</option></select></label>
        <label class="f">Sort by
          <select name="sort">
            <option value="spend">total spend</option>
            <option value="name">name</option>
          </select></label>
      </div>
      <div class="lobby-list"></div>`;
    const sectorSel = this.el.querySelector('[name=sector]');
    const sortSel = this.el.querySelector('[name=sort]');
    sortSel.value = this.sort;
    sectorSel.addEventListener('change', () => { this.sector = sectorSel.value; this.load(); });
    sortSel.addEventListener('change', () => { this.sort = sortSel.value; this.load(); });
    this.load();
  }

  async load() {
    const box = this.el.querySelector('.lobby-list');
    if (!box) return;
    box.innerHTML = '';
    box.appendChild(skeleton(6, { table: true }));
    const rows = await this.bag.api.lobbies({ sector: this.sector || undefined, sort: this.sort });
    if (!box.isConnected) return;
    box.innerHTML = '';
    if (rows === null) {
      box.innerHTML = `<div class="empty">Influence ledger unavailable.
        <span class="why">GET /api/lobbies failed — backend offline or the influence routes have not landed yet</span></div>`;
      return;
    }
    // sector dropdown learns its options from the unfiltered result set
    if (this.sectors === null && !this.sector) {
      this.sectors = [...new Set(rows.map((r) => r.sector).filter(Boolean))].sort();
      const sel = this.el.querySelector('[name=sector]');
      if (sel) {
        for (const s of this.sectors) {
          const o = document.createElement('option');
          o.value = s; o.textContent = s;
          sel.appendChild(o);
        }
      }
    }
    if (!rows.length) {
      box.innerHTML = `<div class="empty">No organizations${this.sector ? ` in sector “${escapeHtml(this.sector)}”` : ''} yet.
        <span class="why">rows appear as the LDA/FEC influence sync and the curated seed land</span></div>`;
      return;
    }
    const list = document.createElement('div');
    list.className = 'lobby-rows';
    for (const r of rows) {
      const row = document.createElement('div');
      row.className = 'lobby-row';
      row.innerHTML = `
        <span class="lr-name">${escapeHtml(r.name)}
          <div style="margin-top:3px">
            ${r.sector ? `<span class="chip accent">${escapeHtml(r.sector)}</span>` : ''}
            ${r.org_type ? `<span class="chip">${icon('org')}${escapeHtml(r.org_type)}</span>` : ''}
          </div></span>
        <span class="lr-backed">${r.candidates_backed ? `${r.candidates_backed} candidate${r.candidates_backed === 1 ? '' : 's'} backed` : ''}</span>
        <span class="lr-spend" title="${'$' + Number(r.total_spend || 0).toLocaleString()} — LDA lobbying + FEC independent expenditures">${fmtMoney(r.total_spend)}</span>`;
      row.addEventListener('click', () => this.bag.navigate(`#/lobby/${r.id}`));
      list.appendChild(row);
    }
    box.appendChild(list);
    box.insertAdjacentHTML('beforeend',
      `<div class="lobby-cite">${rows.length} organization(s) · totals are LDA-disclosed lobbying + FEC independent expenditures — never estimates</div>`);
  }
}
