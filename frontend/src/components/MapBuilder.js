/* MapBuilder.js — the 270ToWin-style scenario builder (#/builder), run ON THE
   SAME renderer as the live map (a 'builder' interaction mode: clicks cycle a
   unit's outcome instead of opening the pane). Fresh entries (no URL payload)
   PREFILL from the live model (forecast → average_margin → all-Tossup), and
   the legend swatches double as PAINT BRUSHES: arm one, click units to paint,
   Escape (or re-click) to disarm.

   Outcome gradient (exactly 8 states = 3 bits/unit):
     0 Safe D · 1 Likely D · 2 Lean D · 3 Tossup · 4 Lean R · 5 Likely R ·
     6 Safe R · 7 Other

   URL ENCODING (per the manual, exactly): a 4-byte header — version (1B),
   race type (1B: 0=president 1=senate 2=governor 3=house), cycle year
   (2B big-endian) — followed by 3 bits per unit in canonical unit order,
   MSB-first, base64url-encoded into the fragment (#/builder/{payload}).
   House stress case: 435×3 bits ≈ 164B + 4B header → ~224 chars.

   Canonical unit order: president = state FIPS ascending, with the ME/NE
   congressional-district sub-units ('23-1','23-2','31-1'…) inserted directly
   after their state (elector_method read from /api/geo/states; vendored 2024
   allocation as offline fallback, honestly labeled). senate/governor/house =
   the cycle's races sorted by (state_fips, district/seat) — only seats up
   that cycle are toggleable. Scenarios save named+dated to localStorage;
   PNG export composites the live canvas + title/legend/totals; CSV/JSON
   export the raw assignments; optional diff-vs-live-forecast overlay. */

import { composeSnapshot, downloadCanvas, downloadText } from './Snapshot.js';
import { escapeHtml } from './PollsWindow.js';
import { themePalette, mix } from './map/geometry.js';

export const OUTCOMES = [
  { key: 'safe_d', label: 'Safe D' }, { key: 'likely_d', label: 'Likely D' },
  { key: 'lean_d', label: 'Lean D' }, { key: 'tossup', label: 'Tossup' },
  { key: 'lean_r', label: 'Lean R' }, { key: 'likely_r', label: 'Likely R' },
  { key: 'safe_r', label: 'Safe R' }, { key: 'other', label: 'Other' },
];
const TOSSUP = 3;
const RACE_TYPES = ['president', 'senate', 'governor', 'house'];
const SAVES_KEY = 'pollgrid.builder.saves';

/* Vendored fallback: 2024-allocation electoral votes + elector method,
   used only when /api/geo/states is unreachable (labeled in the UI). */
const FALLBACK_EV = {
  '01': 9, '02': 3, '04': 11, '05': 6, '06': 54, '08': 10, '09': 7, '10': 3, '11': 3,
  '12': 30, '13': 16, '15': 4, '16': 4, '17': 19, '18': 11, '19': 6, '20': 6, '21': 8,
  '22': 8, '23': 4, '24': 10, '25': 11, '26': 15, '27': 10, '28': 6, '29': 10, '30': 4,
  '31': 5, '32': 6, '33': 4, '34': 14, '35': 5, '36': 28, '37': 16, '38': 3, '39': 17,
  '40': 7, '41': 8, '42': 19, '44': 4, '45': 9, '46': 3, '47': 11, '48': 40, '49': 6,
  '50': 3, '51': 13, '53': 12, '54': 4, '55': 10, '56': 3,
};
const CD_METHOD = { '23': 2, '31': 3 }; // ME: 2 districts, NE: 3

/* ---- bit packing ---- */

function packScenario(version, raceTypeIdx, cycle, values) {
  const bits = values.length * 3;
  const bytes = new Uint8Array(4 + Math.ceil(bits / 8));
  bytes[0] = version; bytes[1] = raceTypeIdx;
  bytes[2] = (cycle >> 8) & 0xff; bytes[3] = cycle & 0xff;
  values.forEach((v, i) => {
    const bit = i * 3;
    for (let b = 0; b < 3; b++) {
      if (v & (1 << (2 - b))) {
        const pos = bit + b;
        bytes[4 + (pos >> 3)] |= 1 << (7 - (pos & 7));
      }
    }
  });
  let bin = '';
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unpackScenario(payload) {
  try {
    const b64 = payload.replace(/-/g, '+').replace(/_/g, '/');
    const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (bytes.length < 4) return null;
    const nUnits = Math.floor(((bytes.length - 4) * 8) / 3);
    const values = [];
    for (let i = 0; i < nUnits; i++) {
      let v = 0;
      for (let b = 0; b < 3; b++) {
        const pos = i * 3 + b;
        if (bytes[4 + (pos >> 3)] & (1 << (7 - (pos & 7)))) v |= 1 << (2 - b);
      }
      values.push(v);
    }
    return { version: bytes[0], raceTypeIdx: bytes[1], cycle: (bytes[2] << 8) | bytes[3], values };
  } catch (e) { return null; }
}

/* ---- the builder ---- */

export class MapBuilder {
  /**
   * @param {HTMLElement} paneRoot left pane element the builder renders into
   * @param {object} bag {api, getMap, statesByFips, toast, navigate, setBuilderMode}
   */
  constructor(paneRoot, bag) {
    this.root = paneRoot;
    this.bag = bag;
    this.active = false;
    this.raceType = 'president';
    this.cycle = new Date().getFullYear() + (new Date().getFullYear() % 2 ? 1 : 0);
    this.units = [];              // [{key, label, ev, fixedHolder|null, stateFips, competitive}]
    this.assign = new Map();      // key -> 0..7
    this.usingFallbackEv = false;
    this.diff = null;             // forecast diff overlay data
    this.brush = null;            // armed paint outcome (0..7) | null = click-to-cycle
    this.prefillSource = null;    // 'forecast' | 'average_margin' | null (blank/URL)
    this._onKeydown = (e) => { if (e.key === 'Escape' && this.brush !== null) this._setBrush(null); };
  }

  async activate(payload) {
    this.active = true;
    document.addEventListener('keydown', this._onKeydown);
    let decoded = payload ? unpackScenario(payload) : null;
    if (decoded && RACE_TYPES[decoded.raceTypeIdx]) {
      this.raceType = RACE_TYPES[decoded.raceTypeIdx];
      this.cycle = decoded.cycle || this.cycle;
    }
    await this._loadUnits();
    if (decoded && decoded.values) {
      this.units.forEach((u, i) => {
        if (!u.fixedHolder) this.assign.set(u.key, decoded.values[i] ?? TOSSUP);
      });
      this.prefillSource = null; // explicit scenario from the URL wins
    } else {
      await this._prefillFromModel(); // fresh entry: start from the live model
    }
    this.render();
    this._paint();
  }

  deactivate() {
    this.active = false;
    this.diff = null;
    this._setBrush(null);
    document.removeEventListener('keydown', this._onKeydown);
    const map = this.bag.getMap();
    if (map) map.setOverrideColors('state', null);
  }

  /* ---- prefill from the live model ----
     forecast dem_prob → gradient; fallback to average_margin bands; final
     fallback leaves everything at Tossup (the _loadUnits default). */

  _outcomeFromProb(p) {
    if (p >= 0.90) return 0; // Safe D
    if (p >= 0.75) return 1; // Likely D
    if (p >= 0.60) return 2; // Lean D
    if (p <= 0.10) return 6; // Safe R
    if (p <= 0.25) return 5; // Likely R
    if (p <= 0.40) return 4; // Lean R
    return TOSSUP;
  }

  _outcomeFromMargin(m) {
    if (m >= 10) return 0;  if (m >= 5) return 1;  if (m >= 2) return 2;
    if (m <= -10) return 6; if (m <= -5) return 5; if (m <= -2) return 4;
    return TOSSUP;
  }

  /** Look a unit up in a /api/map/values map (state fips, or district geoid). */
  _valueForUnit(u, values) {
    if (values[u.key] !== undefined) return values[u.key];
    if (u.key.includes('-')) { // 'SS-D' unit key vs 'SSDD' district geoid
      const [fips, dn] = u.key.split('-');
      for (const k of [fips + String(dn).padStart(2, '0'), fips + dn]) {
        if (values[k] !== undefined) return values[k];
      }
    }
    return undefined;
  }

  async _prefillFromModel() {
    this.prefillSource = null;
    const tier = this.raceType === 'house' ? 'district' : 'state';
    const extra = { race_type: this.raceType, cycle: this.cycle };
    const attempts = [
      ['forecast', (v) => this._outcomeFromProb(+v)],
      ['average_margin', (v) => this._outcomeFromMargin(+v)],
    ];
    for (const [mode, toOutcome] of attempts) {
      const res = await this.bag.api.mapValues(mode, tier, extra);
      if (!res || !res.values || !Object.keys(res.values).length) continue;
      let hits = 0;
      for (const u of this.units) {
        if (u.fixedHolder) continue;
        const v = this._valueForUnit(u, res.values);
        if (v === undefined || v === null) continue;
        this.assign.set(u.key, toOutcome(v));
        hits++;
      }
      if (hits) { this.prefillSource = mode; return; }
    }
    // both modes empty → all-Tossup (already the default)
  }

  /* ---- paint mode (legend swatches are brushes) ---- */

  _setBrush(idx) {
    this.brush = (idx === null || this.brush === idx) ? null : idx;
    this._paintBrushUi();
  }

  _paintBrushUi() {
    document.body.classList.toggle('brush-armed', this.active && this.brush !== null);
    if (!this.root || !this.active) return;
    for (const b of this.root.querySelectorAll('.builder-legend .bl')) {
      b.classList.toggle('armed', +b.dataset.idx === this.brush);
    }
    const hint = this.root.querySelector('.brush-hint');
    if (hint) {
      hint.textContent = this.brush === null
        ? 'Click a unit to cycle its outcome — or click a legend swatch to arm it as a brush.'
        : `Painting "${OUTCOMES[this.brush].label}" — click units (or district chips) to paint; click the swatch again or press Escape to stop.`;
    }
  }

  /** Route a map pick into the builder: paint (brush armed) or cycle.
      Zoomed-in county picks resolve to their parent state — scenario units
      are states/seats, so painting keeps working at any zoom level. */
  handlePick(unit) {
    if (!unit) return;
    let key = unit.key;
    if (unit.tier === 'county') key = unit.stateFips || String(unit.key).slice(0, 2);
    else if (unit.tier !== 'state') return;
    const u = this.units.find((x) => x.key === key);
    if (!u) { this.bag.toast('That unit is not part of this scenario'); return; }
    if (u.fixedHolder) { this.bag.toast(`${u.label}: not up in ${this.cycle} — fixed at current holder (${u.fixedHolder})`); return; }
    if (this.brush !== null) this.setUnit(u.key, this.brush);
    else this.cycleUnit(u.key);
  }

  cycleUnit(key) {
    const cur = this.assign.get(key) ?? TOSSUP;
    this.assign.set(key, (cur + 1) % 8);
    this._afterChange();
  }

  setUnit(key, outcome) {
    this.assign.set(key, outcome);
    this._afterChange();
  }

  _afterChange() {
    this._paint();
    this._renderTotals();
    this._renderChips();
    // keep the shareable payload in the fragment without spamming history
    const payload = this.encode();
    history.replaceState(null, '', `#/builder/${payload}`);
  }

  encode() {
    return packScenario(1, RACE_TYPES.indexOf(this.raceType), this.cycle,
      this.units.map((u) => this.assign.get(u.key) ?? TOSSUP));
  }

  /* ---- unit universes per race type ---- */

  async _loadUnits() {
    this.assign = new Map();
    this.units = [];
    this.notice = null;
    if (this.raceType === 'president') await this._loadPresident();
    else if (this.raceType === 'house') await this._loadHouse();
    else await this._loadSeats(this.raceType);
    for (const u of this.units) if (!u.fixedHolder) this.assign.set(u.key, TOSSUP);
  }

  async _loadPresident() {
    const rows = await this.bag.api.geoStates();
    this.usingFallbackEv = !rows;
    const evOf = (fips) => {
      if (rows) {
        const r = rows.find((s) => s.fips_code === fips);
        return r ? { ev: r.electoral_votes || 0, method: r.elector_method, name: r.name } : null;
      }
      return fips in FALLBACK_EV
        ? { ev: FALLBACK_EV[fips], method: CD_METHOD[fips] ? 'congressional_district' : 'winner_take_all', name: this.bag.statesByFips[fips]?.name }
        : null;
    };
    const fipsList = Object.keys(this.bag.statesByFips).sort();
    for (const fips of fipsList) {
      const info = evOf(fips);
      if (!info || !info.ev) continue; // territories carry no electors
      const name = info.name || this.bag.statesByFips[fips]?.name || fips;
      if (info.method === 'congressional_district') {
        const nCds = CD_METHOD[fips] || 2;
        this.units.push({ key: fips, label: `${name} (statewide)`, ev: 2, stateFips: fips });
        for (let d = 1; d <= nCds; d++) {
          this.units.push({ key: `${fips}-${d}`, label: `${name} CD-${d}`, ev: 1, stateFips: fips, isCd: true });
        }
      } else {
        this.units.push({ key: fips, label: name, ev: info.ev, stateFips: fips });
      }
    }
  }

  async _loadSeats(type) {
    const races = await this.bag.api.races({ cycle: this.cycle, type });
    if (races && races.length) {
      const sorted = [...races].sort((a, b) =>
        String(a.state_fips).localeCompare(String(b.state_fips)) || (a.seat || '').localeCompare(b.seat || ''));
      for (const r of sorted) {
        this.units.push({
          key: String(r.state_fips).padStart(2, '0'),
          raceId: r.id,
          label: r.name || `${this.bag.statesByFips[r.state_fips]?.name} ${type}`,
          ev: 1, stateFips: String(r.state_fips).padStart(2, '0'),
          competitive: r.competitiveness && r.competitiveness !== 'safe',
        });
      }
      if (type === 'senate') {
        // seats NOT up this cycle appear fixed at their current holder (real
        // history where known; '?' otherwise) and count toward the topline
        const holders = (await this.bag.api.tryGet('/api/geo/senate_holders')) || {};
        const up = new Set(sorted.map((r) => String(r.state_fips).padStart(2, '0')));
        for (const fips of Object.keys(this.bag.statesByFips).sort()) {
          if (fips === '72' || fips === '11' || up.has(fips)) continue;
          const holder = holders[fips] || '?';
          this.units.push({
            key: `fixed-${fips}`, label: `${this.bag.statesByFips[fips].name} (not up)`,
            ev: 1, stateFips: fips, fixedHolder: holder,
          });
        }
        this.notice = `Only the ${sorted.length} seats up in ${this.cycle} are toggleable; the rest are fixed at their current holder (from certified history where imported, '?' otherwise).`;
      } else {
        this.notice = null;
      }
    } else {
      // degraded: no race data — every state toggleable, honestly labeled
      for (const fips of Object.keys(this.bag.statesByFips).sort()) {
        if (fips === '72') continue;
        this.units.push({ key: fips, label: this.bag.statesByFips[fips].name, ev: 1, stateFips: fips });
      }
      this.notice = `Live ${type} seat data unavailable (GET /api/races?cycle=${this.cycle}&type=${type} failed) — showing every state as toggleable, which a real ${type} map would not.`;
    }
  }

  async _loadHouse() {
    const races = await this.bag.api.races({ cycle: this.cycle, type: 'house' });
    if (races && races.length) {
      const sorted = [...races].sort((a, b) =>
        String(a.state_fips).localeCompare(String(b.state_fips)) || (a.district_number || 0) - (b.district_number || 0));
      for (const r of sorted) {
        this.units.push({
          key: `${String(r.state_fips).padStart(2, '0')}-${r.district_number ?? 0}`,
          raceId: r.id,
          label: r.name || `${this.bag.statesByFips[r.state_fips]?.name}-${r.district_number}`,
          ev: 1, stateFips: String(r.state_fips).padStart(2, '0'), isCd: true,
          competitive: r.competitiveness && r.competitiveness !== 'safe',
          projectedHolder: r.leader_party || null,
        });
      }
    } else {
      this.units = [];
      this.notice = 'House scenarios need the 435-district race list (GET /api/races?type=house) — unavailable while the backend is offline.';
    }
  }

  /* ---- colors & totals ---- */

  /** Legible text color for an arbitrary computed background (light themes). */
  _textOn(c) {
    return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) > 150 ? '#16181c' : '#ffffff';
  }

  _outcomeColor(v, pal) {
    switch (v) {
      case 0: return pal.dem;
      case 1: return mix(pal.dem, pal.bg, 0.25);
      case 2: return mix(pal.dem, pal.bg, 0.5);
      case 3: return mix(pal.neutral, pal.bg, 0.2);
      case 4: return mix(pal.rep, pal.bg, 0.5);
      case 5: return mix(pal.rep, pal.bg, 0.25);
      case 6: return pal.rep;
      case 7: return pal.other;
      default: return pal.low;
    }
  }

  _paint() {
    const map = this.bag.getMap();
    if (!map) return;
    const pal = themePalette();
    const colors = new Map();
    for (const u of this.units) {
      if (u.isCd) continue; // CD sub-units are chips, not map polygons
      const v = this.assign.get(u.key);
      if (v !== undefined) colors.set(u.key, this._outcomeColor(v, pal));
    }
    map.setOverrideColors('state', colors.size ? colors : null);
  }

  totals() {
    const t = { d: 0, r: 0, tossup: 0, other: 0 };
    for (const u of this.units) {
      if (u.fixedHolder) { // not-up seat: counts by its current holder
        if (u.fixedHolder === 'DEM') t.d += u.ev;
        else if (u.fixedHolder === 'REP') t.r += u.ev;
        else t.tossup += u.ev; // unknown holder — honestly uncounted for either side
        continue;
      }
      const v = this.assign.get(u.key) ?? TOSSUP;
      if (v <= 2) t.d += u.ev;
      else if (v === 3) t.tossup += u.ev;
      else if (v <= 6) t.r += u.ev;
      else t.other += u.ev;
    }
    return t;
  }

  _target() {
    return this.raceType === 'president' ? { total: 538, win: 270, unit: 'EV' }
      : this.raceType === 'senate' ? { total: 100, win: 51, unit: 'seats' }
      : this.raceType === 'house' ? { total: 435, win: 218, unit: 'seats' }
      : { total: this.units.length, win: Math.floor(this.units.length / 2) + 1, unit: 'races' };
  }

  /* ---- UI ---- */

  render() {
    if (!this.active) return;
    this.root.classList.add('open');
    const pal = themePalette();
    this.root.innerHTML = `
      <div class="pane-head">
        <b>Map builder</b><span class="spacer"></span>
        <button data-act="exit" title="Exit builder">✕</button>
      </div>
      <div class="pane-body">
        <div class="row">
          <label class="f">Race type
            <select data-f="raceType">${RACE_TYPES.map((t) => `<option ${t === this.raceType ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
          <label class="f">Cycle
            <select data-f="cycle">${[2028, 2026, 2024, 2022, 2020].map((c) => `<option ${c === this.cycle ? 'selected' : ''}>${c}</option>`).join('')}</select></label>
        </div>
        ${this.usingFallbackEv ? `<div class="dim" style="font-size:10px;margin-top:4px">live elector data unavailable — using the vendored 2024 allocation</div>` : ''}
        ${this.notice ? `<div class="empty" style="text-align:left">${escapeHtml(this.notice)}</div>` : ''}
        <div class="dim prefill-note" style="font-size:10px;margin-top:4px">${this.prefillSource
          ? `prefilled from the live model (${this.prefillSource === 'forecast' ? 'forecast win probabilities' : 'polling-average margins'})`
          : ''}</div>
        <div class="builder-totals mt" title="live topline"></div>
        <div class="topline-note dim" style="font-size:10px;margin-top:3px"></div>
        <div class="builder-legend mt" title="click a swatch to arm it as a paint brush">
          ${OUTCOMES.map((o, i) => {
            const c = this._outcomeColor(i, pal);
            return `<button class="bl" data-idx="${i}" title="arm '${o.label}' as a brush"><span class="sw" style="background:rgb(${c[0]},${c[1]},${c[2]})"></span>${o.label}</button>`;
          }).join('')}
        </div>
        <p class="dim brush-hint" style="font-size:11px">Click a unit to cycle its outcome — or click a legend swatch to arm it as a brush.</p>
        <div class="row" style="font-size:11px">
          <span class="dim">Reset:</span>
          <button data-act="reset-blank" title="set every toggleable unit to Tossup">blank</button>
          <button data-act="reset-model" title="re-prefill from the live model (forecast, then average-margin fallback)">from model</button>
        </div>
        <div class="builder-chips"></div>
        <div class="panel"><div class="panel-head">Save / share / export</div><div class="panel-body">
          <div class="row">
            <input data-f="savename" placeholder="scenario name…" size="16">
            <button data-act="save">Save</button>
          </div>
          <div class="saves mt"></div>
          <div class="row mt">
            <button data-act="copy">Copy share link</button>
            <button data-act="png">PNG</button>
            <button data-act="csv">CSV</button>
            <button data-act="json">JSON</button>
            <button data-act="diff">Diff vs forecast</button>
          </div>
          <div class="diff-out mt"></div>
        </div></div>
      </div>`;

    this.root.querySelector('[data-act=exit]').addEventListener('click', () => this.bag.navigate('#/'));
    this.root.querySelector('[data-f=raceType]').addEventListener('change', async (e) => {
      this.raceType = e.target.value;
      await this._loadUnits(); await this._prefillFromModel(); this.render(); this._afterChange();
    });
    this.root.querySelector('[data-f=cycle]').addEventListener('change', async (e) => {
      this.cycle = +e.target.value;
      await this._loadUnits(); await this._prefillFromModel(); this.render(); this._afterChange();
    });
    for (const b of this.root.querySelectorAll('.builder-legend .bl')) {
      b.addEventListener('click', () => this._setBrush(+b.dataset.idx));
    }
    this.root.querySelector('[data-act=reset-blank]').addEventListener('click', () => {
      for (const u of this.units) if (!u.fixedHolder) this.assign.set(u.key, TOSSUP);
      this.prefillSource = null;
      const note = this.root.querySelector('.prefill-note');
      if (note) note.textContent = '';
      this._afterChange();
    });
    this.root.querySelector('[data-act=reset-model]').addEventListener('click', async () => {
      for (const u of this.units) if (!u.fixedHolder) this.assign.set(u.key, TOSSUP);
      await this._prefillFromModel();
      const note = this.root.querySelector('.prefill-note');
      if (note) {
        note.textContent = this.prefillSource
          ? `prefilled from the live model (${this.prefillSource === 'forecast' ? 'forecast win probabilities' : 'polling-average margins'})`
          : 'live model unavailable — reset to all-Tossup';
      }
      this._afterChange();
    });
    this.root.querySelector('[data-act=save]').addEventListener('click', () => this._save());
    this.root.querySelector('[data-act=copy]').addEventListener('click', async () => {
      const url = `${location.origin}${location.pathname}#/builder/${this.encode()}`;
      try { await navigator.clipboard.writeText(url); this.bag.toast('Share link copied'); }
      catch (e) { this.bag.toast(url); }
    });
    this.root.querySelector('[data-act=png]').addEventListener('click', () => this._exportPng());
    this.root.querySelector('[data-act=csv]').addEventListener('click', () => {
      const lines = ['unit,label,ev,outcome'];
      for (const u of this.units) lines.push(`${u.key},"${u.label}",${u.ev},${OUTCOMES[this.assign.get(u.key) ?? TOSSUP].key}`);
      downloadText(lines.join('\n'), `pollgrid-scenario-${this.raceType}-${this.cycle}.csv`, 'text/csv');
    });
    this.root.querySelector('[data-act=json]').addEventListener('click', () => {
      downloadText(JSON.stringify({
        race_type: this.raceType, cycle: this.cycle, payload: this.encode(),
        assignments: this.units.map((u) => ({ unit: u.key, label: u.label, ev: u.ev, outcome: OUTCOMES[this.assign.get(u.key) ?? TOSSUP].key })),
      }, null, 2), `pollgrid-scenario-${this.raceType}-${this.cycle}.json`, 'application/json');
    });
    this.root.querySelector('[data-act=diff]').addEventListener('click', () => this._diffForecast());

    this._renderTotals();
    this._renderChips();
    this._renderSaves();
    this._paintBrushUi();
  }

  _renderTotals() {
    const bar = this.root.querySelector('.builder-totals');
    if (!bar) return;
    const t = this.totals();
    const tgt = this._target();
    const total = Math.max(1, t.d + t.r + t.tossup + t.other);
    const pal = themePalette();
    const seg = (v, color) => v ? `<span class="seg" style="flex:${v};background:rgb(${color[0]},${color[1]},${color[2]});color:${this._textOn(color)}">${v}</span>` : '';
    bar.innerHTML =
      seg(t.d, pal.dem) + seg(t.tossup, mix(pal.neutral, pal.bg, 0.2)) + seg(t.other, pal.other) + seg(t.r, pal.rep);
    const note = this.root.querySelector('.topline-note');
    note.textContent = `D ${t.d} · Tossup ${t.tossup} · Other ${t.other} · R ${t.r}  —  ${tgt.win} ${tgt.unit} to win` +
      (t.d >= tgt.win ? '  ✓ D majority' : t.r >= tgt.win ? '  ✓ R majority' : '');
  }

  _renderChips() {
    const box = this.root.querySelector('.builder-chips');
    if (!box) return;
    const pal = themePalette();
    const cdUnits = this.units.filter((u) => u.isCd);
    box.innerHTML = '';
    if (!cdUnits.length) return;
    const groups = {};
    for (const u of cdUnits) (groups[u.stateFips] ||= []).push(u);
    for (const [fips, us] of Object.entries(groups)) {
      const competitive = us.filter((u) => u.competitive !== false);
      const safe = us.filter((u) => u.competitive === false);
      const g = document.createElement('div');
      g.className = 'mt';
      g.innerHTML = `<div class="dim" style="font-size:10px">${escapeHtml(this.bag.statesByFips[fips]?.name || fips)}</div>`;
      const rowEl = document.createElement('div');
      rowEl.className = 'row';
      const chipFor = (u) => {
        const v = this.assign.get(u.key) ?? TOSSUP;
        const c = this._outcomeColor(v, pal);
        const chip = document.createElement('span');
        chip.className = 'dchip';
        chip.style.background = `rgb(${c[0]},${c[1]},${c[2]})`;
        chip.style.color = this._textOn(c);
        chip.textContent = `${u.key} ${OUTCOMES[v].label}`;
        chip.title = u.label;
        // chips accept the armed brush too, otherwise click-to-cycle
        chip.addEventListener('click', () => (this.brush !== null ? this.setUnit(u.key, this.brush) : this.cycleUnit(u.key)));
        return chip;
      };
      for (const u of competitive) rowEl.appendChild(chipFor(u));
      g.appendChild(rowEl);
      if (safe.length) {
        // safe seats collapse to their projected holder — expandable
        const det = document.createElement('details');
        det.innerHTML = `<summary class="dim" style="font-size:10px;cursor:pointer">${safe.length} safe seats (collapsed to projected holder)</summary>`;
        const safeRow = document.createElement('div');
        safeRow.className = 'row';
        for (const u of safe) safeRow.appendChild(chipFor(u));
        det.appendChild(safeRow);
        g.appendChild(det);
      }
      box.appendChild(g);
    }
  }

  /* ---- persistence ---- */

  _loadSaves() {
    try { return JSON.parse(localStorage.getItem(SAVES_KEY)) || []; } catch (e) { return []; }
  }

  _save() {
    const name = this.root.querySelector('[data-f=savename]').value.trim() || `${this.raceType} ${this.cycle}`;
    const saves = this._loadSaves();
    saves.unshift({ name, date: new Date().toISOString().slice(0, 10), payload: this.encode() });
    try { localStorage.setItem(SAVES_KEY, JSON.stringify(saves.slice(0, 30))); } catch (e) { /* full */ }
    this.bag.toast(`Saved "${name}"`);
    this._renderSaves();
  }

  _renderSaves() {
    const box = this.root.querySelector('.saves');
    if (!box) return;
    const saves = this._loadSaves();
    box.innerHTML = saves.length ? '' : '<span class="dim" style="font-size:11px">no saved scenarios yet</span>';
    saves.forEach((s, i) => {
      const rowEl = document.createElement('div');
      rowEl.className = 'kv';
      rowEl.innerHTML = `<span class="k"><a href="#/builder/${s.payload}">${escapeHtml(s.name)}</a> <span class="dim">${s.date}</span></span>`;
      const del = document.createElement('button');
      del.textContent = '✕';
      del.title = 'delete scenario';
      del.addEventListener('click', () => {
        saves.splice(i, 1);
        try { localStorage.setItem(SAVES_KEY, JSON.stringify(saves)); } catch (e) { /* noop */ }
        this._renderSaves();
      });
      rowEl.appendChild(del);
      box.appendChild(rowEl);
    });
  }

  /* ---- exports & diff ---- */

  _exportPng() {
    const map = this.bag.getMap();
    const canvas = map && map.getCanvas ? map.getCanvas() : null;
    const pal = themePalette();
    const t = this.totals();
    const out = composeSnapshot(canvas, {
      title: `Scenario — ${this.raceType} ${this.cycle}`,
      subtitle: `built on PollGrid · ${new Date().toISOString().slice(0, 10)}`,
      legend: OUTCOMES.map((o, i) => {
        const c = this._outcomeColor(i, pal);
        return { color: `rgb(${c[0]},${c[1]},${c[2]})`, label: o.label };
      }),
      totals: [`D ${t.d}`, `R ${t.r}`, `Tossup ${t.tossup}`, `Other ${t.other}`],
    });
    downloadCanvas(out, `pollgrid-${this.raceType}-${this.cycle}.png`);
  }

  async _diffForecast() {
    const out = this.root.querySelector('.diff-out');
    out.innerHTML = '<span class="dim">comparing…</span>';
    const fc = await this.bag.api.mapValues('forecast', 'state', { cycle: this.cycle, race_type: this.raceType });
    if (!fc || !fc.values) {
      out.innerHTML = '<div class="empty">Live forecast unavailable to diff against.<span class="why">GET /api/map/values?mode=forecast failed</span></div>';
      return;
    }
    const disagreements = [];
    for (const u of this.units) {
      if (u.isCd) continue;
      const mine = this.assign.get(u.key) ?? TOSSUP;
      const prob = fc.values[u.key];
      if (prob === undefined || mine === TOSSUP || mine === 7) continue;
      const modelSaysD = prob > 0.5;
      const iSayD = mine <= 2;
      if (modelSaysD !== iSayD) {
        disagreements.push(`${u.label}: you ${OUTCOMES[mine].label}, model ${(prob * 100).toFixed(0)}% D`);
      }
    }
    out.innerHTML = disagreements.length
      ? `<div class="dim" style="font-size:11px">You and the live forecast disagree on ${disagreements.length} unit(s):</div>`
        + disagreements.map((d) => `<div style="font-size:11px">· ${escapeHtml(d)}</div>`).join('')
      : '<div class="dim" style="font-size:11px">No disagreements with the live forecast on assigned units.</div>';
  }
}
