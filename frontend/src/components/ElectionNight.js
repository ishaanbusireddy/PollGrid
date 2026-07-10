/* ElectionNight.js — Election Night Mode. Activated per race (from a race
   page) or globally when /api/status reports election_night_mode. The camera
   flies to the race's geography, counties fill live from
   /api/electionnight/live (polled every 15s in this mode), and the panel
   splits into results / turnout / fundraising / media tabs.

   Honesty rules enforced visually:
   - "CALLABLE — awaiting human review" pulses amber; "CALLED by [named
     human]" is steady and names the caller. No automated call path exists.
   - The call form POSTs /api/electionnight/call (the server rejects
     system/model identities).
   - The results source tier is labeled plainly ("results via [State] SoS"
     vs "results via AP Elections"). */

import { escapeHtml } from './PollsWindow.js';
import { themePalette } from './map/geometry.js';

const POLL_MS = 15000;

export class ElectionNight {
  /**
   * @param {HTMLElement} el mount point (over the map)
   * @param {{api:object, getMap:Function, flyToState:Function, navigate:Function,
   *          onCountyColors:Function, playFanfare:Function, toast:Function}} bag
   */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.active = false;
    this.raceId = null;
    this.tab = 'results';
    this.data = null;
    this._timer = null;
    this._lastCalled = new Set();
  }

  activate(raceId) {
    this.active = true;
    this.raceId = raceId ?? null;
    this.tab = 'results';
    this.render();
    this.refresh();
    clearInterval(this._timer);
    this._timer = setInterval(() => this.refresh(), POLL_MS);
  }

  deactivate() {
    this.active = false;
    clearInterval(this._timer);
    this.el.innerHTML = '';
    this.bag.onCountyColors(null);
  }

  async refresh() {
    if (!this.active) return;
    this.data = await this.bag.api.electionNightLive(this.raceId);
    if (!this.active) return;
    this._applyMapFill();
    this._renderBody();
  }

  _races() { return (this.data && this.data.races) || []; }

  _applyMapFill() {
    const races = this._races();
    if (!races.length) { this.bag.onCountyColors(null); return; }
    const pal = themePalette();
    const partyColor = (p) => (p === 'DEM' ? pal.dem : p === 'REP' ? pal.rep : pal.other);
    const colors = new Map();
    let flyState = null;
    for (const r of races) {
      for (const c of r.counties || []) {
        const votes = c.party_votes || {};
        let lead = null, max = -1;
        for (const [p, v] of Object.entries(votes)) if (v > max) { max = v; lead = p; }
        if (!lead) continue;
        const base = partyColor(lead);
        const rep = Math.max(0.15, Math.min(1, (c.pct_reporting || 0) / 100));
        // fade from background toward the party color as reporting climbs
        colors.set(c.geoid, base.map((v, i) => Math.round(pal.bg[i] + (v - pal.bg[i]) * rep)));
        if (!flyState && c.geoid) flyState = String(c.geoid).slice(0, 2);
      }
    }
    this.bag.onCountyColors(colors.size ? colors : null);
    if (flyState && !this._flew) { this._flew = true; this.bag.flyToState(flyState); }
    // fanfare on a newly-called race
    for (const r of races) {
      if (r.called && !this._lastCalled.has(r.race_id)) {
        this._lastCalled.add(r.race_id);
        this.bag.playFanfare();
      }
    }
  }

  render() {
    this.el.innerHTML = `
      <div class="en-panel">
        <div class="en-head">
          <span class="chip warn">ELECTION NIGHT</span>
          <b style="flex:1">${this.raceId ? 'Race #' + escapeHtml(String(this.raceId)) : 'All live races'}</b>
          <button data-act="close" title="Exit election night mode">✕</button>
        </div>
        <div class="en-tabs">
          ${['results', 'turnout', 'fundraising', 'media'].map((t) =>
            `<button data-tab="${t}" class="${t === this.tab ? 'active' : ''}">${t}</button>`).join('')}
        </div>
        <div class="en-body"><div class="dim">loading live results…</div></div>
      </div>`;
    this.el.querySelector('[data-act=close]').addEventListener('click', () => this.deactivate());
    for (const b of this.el.querySelectorAll('[data-tab]')) {
      b.addEventListener('click', () => {
        this.tab = b.dataset.tab;
        for (const x of this.el.querySelectorAll('[data-tab]')) x.classList.toggle('active', x === b);
        this._renderBody();
      });
    }
  }

  _renderBody() {
    const body = this.el.querySelector('.en-body');
    if (!body) return;
    const races = this._races();
    if (!this.data) {
      body.innerHTML = '<div class="empty">Live results unavailable.<span class="why">GET /api/electionnight/live failed — backend offline</span></div>';
      return;
    }
    if (!races.length) {
      body.innerHTML = '<div class="empty">No live races right now.<span class="why">/api/electionnight/live returned an empty set</span></div>';
      return;
    }
    if (this.tab === 'results') this._renderResults(body, races);
    else if (this.tab === 'turnout') this._renderTurnout(body, races);
    else body.innerHTML = `<div class="empty">${this.tab === 'fundraising'
      ? 'Fundraising context lives on the race page (fundamentals breakdown).'
      : 'Media framing lives on the race page (framing matrix).'}
      <span class="why">open the race for the full ${this.tab} view</span></div>`
      + (this.raceId ? `<a href="#/race/${this.raceId}">open race page →</a>` : '');
  }

  _renderResults(body, races) {
    body.innerHTML = '';
    for (const r of races) {
      const box = document.createElement('div');
      box.className = 'panel';
      const totals = Object.entries(r.total_votes || {})
        .sort((a, b) => b[1] - a[1])
        .map(([p, v]) => `<span class="chip ${p === 'DEM' ? 'dem' : p === 'REP' ? 'rep' : 'other'}">${escapeHtml(p)} ${Number(v).toLocaleString()}</span>`)
        .join(' ');
      const srcLabel = r.source_tier === 'ap' ? 'results via AP Elections'
        : r.source_tier === 'native' ? 'results via State Secretary of State feed'
        : r.source_tier ? `results via ${escapeHtml(r.source_tier)}` : 'source tier unknown';
      let stateChip;
      if (r.called) {
        stateChip = `<span class="state-called">CALLED by ${escapeHtml(r.called.called_by || '?')}${r.called.winner_party ? ' — ' + escapeHtml(r.called.winner_party) : ''}</span>`;
      } else if (r.callable) {
        stateChip = `<span class="state-callable">CALLABLE — awaiting human review</span>`;
      } else {
        stateChip = `<span class="chip">counting</span>`;
      }
      const reporting = (r.counties || []);
      const avgRep = reporting.length
        ? (reporting.reduce((s, c) => s + (c.pct_reporting || 0), 0) / reporting.length).toFixed(0) + '% reporting avg'
        : 'no county detail yet';
      box.innerHTML = `
        <div class="panel-head"><a href="#/race/${r.race_id}">${escapeHtml(r.name || 'race #' + r.race_id)}</a></div>
        <div class="panel-body">
          <div class="row">${stateChip}</div>
          <div class="mt">${totals || '<span class="dim">no votes tallied yet</span>'}</div>
          <div class="dim" style="font-size:11px;margin-top:4px">${reporting.length} counties · ${avgRep}</div>
          <div class="source-tier mt">${srcLabel}</div>
          ${r.callable && !r.called ? this._callFormHtml(r.race_id) : ''}
        </div>`;
      body.appendChild(box);
      const form = box.querySelector('form.en-call');
      if (form) this._bindCallForm(form, r.race_id);
    }
  }

  _callFormHtml(raceId) {
    return `
      <form class="en-call mt" data-race="${raceId}">
        <div class="dim" style="font-size:11px;margin-bottom:4px">Submit a human call — this is never automated:</div>
        <div class="row">
          <select name="winner_party" required>
            <option value="">winner…</option><option>DEM</option><option>REP</option><option>OTHER</option>
          </select>
          <input name="called_by" placeholder="your name (a real human)" required size="16">
        </div>
        <div class="row mt">
          <input name="notes" placeholder="notes (optional)" size="24">
          <button class="primary" type="submit">CALL RACE</button>
        </div>
      </form>`;
  }

  _bindCallForm(form, raceId) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      try {
        await this.bag.api.electionNightCall({
          race_id: raceId,
          winner_party: fd.get('winner_party'),
          called_by: fd.get('called_by'),
          notes: fd.get('notes') || '',
        });
        this.bag.toast(`Race call submitted by ${fd.get('called_by')}`);
        this.bag.playFanfare();
        this.refresh();
      } catch (err) {
        this.bag.toast(`Call rejected: ${err.message}`);
      }
    });
  }

  _renderTurnout(body, races) {
    body.innerHTML = '';
    for (const r of races) {
      const total = Object.values(r.total_votes || {}).reduce((a, b) => a + b, 0);
      const box = document.createElement('div');
      box.className = 'panel';
      box.innerHTML = `
        <div class="panel-head">${escapeHtml(r.name || 'race #' + r.race_id)}</div>
        <div class="panel-body">
          <div class="kv"><span class="k">Votes counted</span><span class="v">${total.toLocaleString()}</span></div>
          ${(r.counties || []).slice(0, 12).map((c) => `
            <div class="bar-row">
              <span class="bar-label mono">${escapeHtml(c.geoid)}</span>
              <span class="bar-track"><span class="bar-fill" style="width:${Math.min(100, c.pct_reporting || 0)}%"></span></span>
              <span class="bar-val">${(c.pct_reporting || 0).toFixed(0)}%</span>
            </div>`).join('') || '<div class="dim">no county detail</div>'}
        </div>`;
      body.appendChild(box);
    }
  }
}
