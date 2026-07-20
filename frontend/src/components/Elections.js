/* Elections.js — (#/elections) the 2026 election calendar: every state's
   primary and the November general on one chronological timeline, with the
   next date up highlighted, past dates dimmed, and each state chip clickable
   through to its state page. Data: GET /api/elections (hand-checked calendar
   + dated races), plus quick cycle stat tiles. */

import { escapeHtml } from './PollsWindow.js';
import { skeleton } from '../anim.js';

const KIND_LABEL = { primary: 'Primary', runoff: 'Runoff', general: 'General election' };

function fmtDate(d) {
  return new Date(d + 'T12:00:00').toLocaleDateString(undefined,
    { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
}

export class Elections {
  /** @param {HTMLElement} el @param {{api:object}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
  }

  async show() {
    this.el.innerHTML = `<h1>2026 Elections</h1>
      <p class="dim">Every state's primary and the November 3 general, chronologically. Click a state to open its page — its Elections tab carries the same dates plus every race on that ballot.</p>
      <div class="elections-stats row" style="align-items:stretch"></div>
      <div class="elections-timeline"></div>`;
    this.el.querySelector('.elections-timeline').appendChild(skeleton(6));

    const [cal, races] = await Promise.all([
      this.bag.api.elections(),
      this.bag.api.racesByPhase({ cycle: 2026, phase: 'general' }),
    ]);
    const tl = this.el.querySelector('.elections-timeline');
    if (!tl) return; // navigated away
    tl.innerHTML = '';

    // cycle stat tiles
    const stats = this.el.querySelector('.elections-stats');
    if (races && races.length) {
      const n = (t) => races.filter((r) => r.race_type === t).length;
      const tile = (v, lab) => `<div class="glance-tile" style="min-width:110px"><div class="g-val">${v}</div><div class="g-lab">${lab}</div></div>`;
      stats.innerHTML = tile(n('house'), 'House seats') + tile(n('senate'), 'Senate races')
        + tile(n('governor'), 'Governorships') + tile('Nov 3', 'General election');
    }

    if (!cal || !(cal.entries || []).length) {
      tl.appendChild(Object.assign(document.createElement('div'), {
        className: 'empty', textContent: 'Election calendar unavailable — backend offline.' }));
      return;
    }
    const todayIso = new Date().toISOString().slice(0, 10);
    let nextMarked = false;
    for (const e of cal.entries) {
      const past = e.date < todayIso;
      const isNext = !past && !nextMarked && (nextMarked = true);
      const card = document.createElement('div');
      card.className = `cal-card ${past ? 'past' : 'upcoming'} ${isNext ? 'next-up' : ''}`;
      card.innerHTML = `
        <div class="cal-card-date">
          <div class="ccd-day">${escapeHtml(fmtDate(e.date))}</div>
          <span class="chip ${e.kind === 'general' ? 'accent' : ''}">${escapeHtml(KIND_LABEL[e.kind] || e.kind)}</span>
          ${isNext ? '<span class="chip ok">next up</span>' : ''}
          ${past ? '<span class="chip">held</span>' : ''}
        </div>
        <div class="cal-card-states"></div>`;
      const box = card.querySelector('.cal-card-states');
      for (const s of e.states) {
        const a = document.createElement('a');
        a.href = `#/state/${s.fips}`;
        a.className = 'territory-chip chip';
        a.title = s.name;
        a.textContent = s.usps;
        box.appendChild(a);
      }
      tl.appendChild(card);
    }
    tl.insertAdjacentHTML('beforeend',
      `<div class="dim" style="font-size:10px;margin-top:8px">dates from the published 2026 state primary calendars — runoff states (AL, AR, GA, MS, OK, SC, TX) may add runoff rounds per contest</div>`);
  }
}
