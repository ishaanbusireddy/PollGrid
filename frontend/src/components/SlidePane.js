/* SlidePane.js — the single left-docked sliding pane hosting EVERY detail
   view (race / state / county / district / candidate / party) with a real
   navigation history (back/forward stack). Pane slide uses CSS transitions.

   Each page composes: header, embedded pre-filtered poll list, Articles
   panel, Electoral History panel (per-office cycle series as inline SVG
   trend charts with boundary-redraw markers), and a demographics panel with
   confidence badges ('derived' always shows its badge — the precinct-honesty
   rule). Race pages add: average + trend sparkline, fundamentals breakdown
   bars, forecast card (shows gate_reason when hidden — honesty), the
   Qualitative Factor Scorecard, corroboration badge, framing mini-heatmap,
   counterfactual launcher, and "Ask the Analyst". */

import { pollsTable, escapeHtml, gradeChip } from './PollsWindow.js';
import { sparkline, trendChart } from '../charts.js';
import { animateNumber, skeleton } from '../anim.js';
import { icon, officeIcon } from '../icons.js';

const OFFICE_LABEL = { president: 'President', senate: 'Senate', governor: 'Governor', house: 'House' };

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** Initials avatar with an optional portrait <img> that removes itself on
    error — a candidate without a loadable portrait_url gets a colored circle
    with two letters, never a broken-image glyph. */
function avatar(name, partyCode, portraitUrl, large = false) {
  const words = String(name || '?').trim().split(/\s+/).filter(Boolean);
  const initials = ((words[0]?.[0] || '?') + (words.length > 1 ? words[words.length - 1][0] : '')).toUpperCase();
  const cls = partyCode === 'DEM' ? 'dem' : partyCode === 'REP' ? 'rep' : '';
  const wrap = el(`<span class="avatar ${cls}${large ? ' lg' : ''}"><span class="avatar-initials">${escapeHtml(initials)}</span></span>`);
  if (portraitUrl) {
    const img = document.createElement('img');
    img.className = 'avatar-img';
    img.alt = '';
    img.loading = 'lazy';
    img.addEventListener('error', () => img.remove());
    img.src = portraitUrl;
    wrap.appendChild(img);
  }
  return wrap;
}

/** Inline-SVG donut. parts: [{label, value, colorVar}] */
function donutSvg(parts, size = 92) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('width', size); svg.setAttribute('height', size);
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  svg.classList.add('mini-svg');
  const total = parts.reduce((a, p) => a + p.value, 0);
  if (!total) return svg;
  const r = size / 2 - 8, cx = size / 2, cy = size / 2;
  const C = 2 * Math.PI * r;
  let offset = 0;
  for (const p of parts) {
    if (!p.value) continue;
    const frac = p.value / total;
    const arc = document.createElementNS(NS, 'circle');
    arc.setAttribute('cx', cx); arc.setAttribute('cy', cy); arc.setAttribute('r', r);
    arc.setAttribute('fill', 'none');
    arc.setAttribute('stroke', p.colorVar);
    arc.setAttribute('stroke-width', 12);
    arc.setAttribute('stroke-dasharray', `${(frac * C).toFixed(2)} ${(C - frac * C).toFixed(2)}`);
    arc.setAttribute('stroke-dashoffset', String((-offset * C + C / 4).toFixed(2)));
    const t = document.createElementNS(NS, 'title');
    t.textContent = `${p.label}: ${p.value} (${(frac * 100).toFixed(0)}%)`;
    arc.appendChild(t);
    svg.appendChild(arc);
    offset += frac;
  }
  const label = document.createElementNS(NS, 'text');
  label.setAttribute('x', cx); label.setAttribute('y', cy + 4);
  label.setAttribute('text-anchor', 'middle');
  label.setAttribute('font-size', 15); label.setAttribute('font-weight', 700);
  label.setAttribute('fill', 'var(--text)'); label.setAttribute('font-family', 'var(--mono)');
  label.textContent = total;
  svg.appendChild(label);
  return svg;
}

function panel(title, extraHead = '') {
  const p = el(`<div class="panel"><div class="panel-head">${title}${extraHead}</div><div class="panel-body"></div></div>`);
  return { root: p, body: p.querySelector('.panel-body'), head: p.querySelector('.panel-head') };
}

function empty(msg, why) {
  return el(`<div class="empty">${msg}${why ? `<span class="why">${escapeHtml(why)}</span>` : ''}</div>`);
}

function partyChipClass(code) {
  return code === 'DEM' ? 'dem' : code === 'REP' ? 'rep' : 'other';
}

export class SlidePane {
  /**
   * @param {HTMLElement} root the #pane element
   * @param {object} bag callbacks: api, navigate, flyToState, flyToCounty,
   *   openAnalyst(type,id,label), activateElectionNight(raceId), toast, statesByFips
   */
  constructor(root, bag) {
    this.root = root;
    this.bag = bag;
    this.stack = [];
    this.pos = -1;
    this.current = null;
  }

  /** Open a view {type, id}; pushes onto the pane's own history stack. */
  open(view, fromHistory = false) {
    if (!fromHistory) {
      this.stack = this.stack.slice(0, this.pos + 1);
      this.stack.push(view);
      this.pos = this.stack.length - 1;
    }
    this.current = view;
    this.root.classList.add('open');
    this._render(view);
  }

  close() {
    this.root.classList.remove('open');
    this.current = null;
  }

  back() { if (this.pos > 0) { this.pos--; this.open(this.stack[this.pos], true); } }
  forward() { if (this.pos < this.stack.length - 1) { this.pos++; this.open(this.stack[this.pos], true); } }

  async _render(view) {
    const token = (this._token = Symbol());
    this.root.innerHTML = `
      <div class="pane-head">
        <button data-nav="back" title="Back" ${this.pos <= 0 ? 'disabled' : ''}>←</button>
        <button data-nav="fwd" title="Forward" ${this.pos >= this.stack.length - 1 ? 'disabled' : ''}>→</button>
        <span class="spacer"></span>
        <button data-nav="watch" class="watch-btn" title="Add to watchlist" hidden>☆</button>
        <span class="chip">${escapeHtml(view.type)}</span>
        <button data-nav="close" title="Close pane">✕</button>
      </div>
      <div class="pane-body"></div>`;
    this.root.querySelector('.pane-body').appendChild(skeleton(7));
    this.root.querySelector('[data-nav=back]').addEventListener('click', () => this.back());
    this.root.querySelector('[data-nav=fwd]').addEventListener('click', () => this.forward());
    this.root.querySelector('[data-nav=close]').addEventListener('click', () => { this.close(); this.bag.navigate('#/'); });
    this._wireWatchButton(view, token);
    const body = this.root.querySelector('.pane-body');

    const pages = {
      race: () => this._racePage(body, view.id),
      state: () => this._statePage(body, view.id),
      county: () => this._countyPage(body, view.id),
      district: () => this._districtPage(body, view.id),
      candidate: () => this._candidatePage(body, view.id),
      party: () => this._partyPage(body, view.id),
      story: () => this._storyPage(body, view.id),
      lobby: () => this._lobbyPage(body, view.id),
    };
    const fn = pages[view.type];
    if (!fn) { body.innerHTML = ''; body.appendChild(empty('Unknown view.')); return; }
    try {
      await fn();
    } catch (e) {
      if (this._token === token) {
        body.innerHTML = '';
        body.appendChild(empty('This view failed to render.', String(e.message || e)));
      }
    }
  }

  /* ---------------- watchlist star (race / state / candidate) ---------------- */

  async _wireWatchButton(view, token) {
    const WATCHABLE = { race: 'race', state: 'state', candidate: 'candidate' };
    const entityType = WATCHABLE[view.type];
    const btn = this.root.querySelector('[data-nav=watch]');
    if (!btn || !entityType) return;
    const rows = await this.bag.api.watchlist();
    if (this._token !== token || !btn.isConnected) return;
    if (rows === null) return; // route unavailable -> feature hidden
    const entityId = String(view.id);
    let watched = rows.some((w) => w.entity_type === entityType && String(w.entity_id) === entityId);
    const paint = () => {
      btn.textContent = watched ? '★' : '☆';
      btn.classList.toggle('on', watched);
      btn.title = watched ? 'Remove from watchlist' : 'Add to watchlist';
    };
    paint();
    btn.hidden = false;
    btn.addEventListener('click', async () => {
      const was = watched;
      watched = !watched; paint(); // optimistic
      try {
        if (watched) await this.bag.api.watchlistAdd(entityType, entityId);
        else await this.bag.api.watchlistDelete(entityType, entityId);
        if (this.bag.refreshWatchlist) this.bag.refreshWatchlist();
      } catch (e) {
        watched = was; paint(); // revert
        this.bag.toast(`Watchlist update failed: ${e.message || e}`);
      }
    });
  }

  /* ---------------- shared sub-panels ---------------- */

  async _articlesPanel(entityType, id) {
    const { root, body } = panel('Articles');
    const rows = await this.bag.api.articles(entityType, id);
    if (!rows || !rows.length) {
      body.appendChild(empty('No articles matched yet.', `GET /api/articles/${entityType}/${id} empty or unavailable`));
      return root;
    }
    for (const a of rows.slice(0, 20)) {
      body.appendChild(el(`
        <div style="margin-bottom:8px">
          <a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(a.title)}</a>
          <div class="dim" style="font-size:11px">${escapeHtml(a.outlet || '?')}
            ${a.reliability_tier ? `· <span class="chip">${icon('tier')}${escapeHtml(String(a.reliability_tier))}</span>` : ''}
            ${a.archival ? `<span class="chip" title="retrieved from an archival source, not the live web">${icon('archival')}archival</span>` : ''}
            ${a.published_at ? '· ' + escapeHtml(String(a.published_at).slice(0, 10)) : ''}</div>
        </div>`));
    }
    return root;
  }

  _geoArticlesNote() {
    const { root, body } = panel('Articles');
    body.appendChild(empty('Articles attach to races, candidates, and parties — not raw geography.',
      'open one of this geography’s races for its article feed'));
    return root;
  }

  async _pollsPanel(filters, scopeNote, title = 'Polls') {
    const { root, body } = panel(title);
    const res = await this.bag.api.polls({ ...filters, limit: 12 });
    body.appendChild(pollsTable(res ? res.rows : null, { scopeNote }));
    body.appendChild(el(`<a href="#/polls" style="font-size:11px">full polls browser →</a>`));
    return root;
  }

  async _historyPanel(tier, id) {
    const { root, body } = panel('Electoral history');
    const h = await this.bag.api.history(tier, id);
    if (!h || !h.rows || !h.rows.length) {
      body.appendChild(empty('No electoral history imported for this entity.',
        `GET /api/entities/${tier}/${id}/history empty or unavailable`));
      return root;
    }
    const markers = (h.boundary_events || []).map((b) => ({
      x: new Date(b.effective_from).getFullYear() || 0,
      label: `Redistricting: Congress ${b.congress_number}${b.note ? ' — ' + b.note : ''}`,
    })).filter((m) => m.x);
    const byOffice = {};
    for (const r of h.rows) (byOffice[r.office] ||= []).push(r);
    for (const [office, rows] of Object.entries(byOffice)) {
      rows.sort((a, b) => a.cycle_year - b.cycle_year);
      const series = rows.map((r) => ({
        x: r.cycle_year,
        y: r.margin_pct != null ? (r.winner_party === 'REP' ? -Math.abs(r.margin_pct) : r.winner_party === 'DEM' ? Math.abs(r.margin_pct) : 0)
          : (r.dem_pct != null && r.rep_pct != null ? r.dem_pct - r.rep_pct : 0),
        note: `turnout ${r.turnout_pct != null ? r.turnout_pct + '%' : '?'} · confidence: ${r.confidence || '?'}`,
      }));
      body.appendChild(el(`<div class="dim" style="font-size:11px;margin-top:6px">${OFFICE_LABEL[office] || office} — margin by cycle (D above line, R below)</div>`));
      body.appendChild(trendChart(series, markers));
      if (rows.some((r) => r.confidence && r.confidence !== 'measured')) {
        body.appendChild(el(`<div class="dim" style="font-size:10px">includes <span class="chip warn">derived/uncertain</span> era-tagged rows — never smoothed to look complete</div>`));
      }
    }
    if (markers.length) {
      body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">dashed verticals mark boundary redraws — results before a redraw belong to that era's geometry</div>`));
    }
    return root;
  }

  async _demographicsPanel(tier, id, opts = {}) {
    const { root, body } = panel('Demographics');
    const d = await this.bag.api.demographics(tier, id);
    if (!d || !d.rows || !d.rows.length) {
      body.appendChild(empty('No demographic panel for this entity.',
        `GET /api/demographics/${tier}/${id} empty or unavailable`));
      if (opts.registration) body.appendChild(this._registrationBlock([]));
      return root;
    }
    if (d.thin_coverage) body.appendChild(el(`<span class="chip warn" title="coverage is genuinely sparse here">thin coverage</span>`));
    const byCat = {};
    for (const r of d.rows) (byCat[r.category] ||= []).push(r);
    // voter registration renders as its own stat-tile block, not a raw kv list
    const regRows = byCat.political_registration || [];
    if (opts.registration || regRows.length) {
      delete byCat.political_registration;
      body.appendChild(this._registrationBlock(regRows));
    }
    for (const [cat, rows] of Object.entries(byCat)) {
      body.appendChild(el(`<div class="dim" style="font-size:11px;margin-top:8px;text-transform:uppercase;letter-spacing:.06em">${escapeHtml(cat)}</div>`));
      for (const r of rows.slice(0, 10)) {
        body.appendChild(el(`
          <div class="kv">
            <span class="k">${escapeHtml(r.variable)}
              ${r.confidence === 'derived' ? '<span class="chip warn" title="apportioned by areal interpolation, not directly measured">derived</span>' : ''}</span>
            <span class="v">${r.value != null ? Number(r.value).toLocaleString() : '—'}</span>
          </div>`));
      }
    }
    body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:6px">as of ${escapeHtml(d.as_of || '?')} · sourced, never LLM-guessed</div>`));
    return root;
  }

  /** Voter-registration block: stat tiles for total/active/inactive plus a
      D/R split bar when the state actually reports by-party registration.
      Honest empty state otherwise — never a fabricated split. */
  _registrationBlock(rows) {
    const box = el(`<div class="mt"><div class="dim" style="font-size:11px;text-transform:uppercase;letter-spacing:.06em">Voter registration</div></div>`);
    const val = (name) => {
      const r = (rows || []).find((x) => x.variable === name);
      return r && r.value != null ? Number(r.value) : null;
    };
    const total = val('registered_total'), active = val('registered_active'), inactive = val('registered_inactive');
    const dem = val('registered_dem'), rep = val('registered_rep');
    if (total == null && active == null && inactive == null && dem == null && rep == null) {
      box.appendChild(empty('No registration statistics for this state yet.',
        'political_registration rows appear once the state registration sync lands'));
      return box;
    }
    const tiles = el(`<div class="stat-tiles"></div>`);
    const tile = (v, lab) => el(`<div class="stat-tile"><div class="st-val">${v != null ? Number(v).toLocaleString() : '—'}</div><div class="st-lab">${lab}</div></div>`);
    if (total != null) tiles.appendChild(tile(total, 'registered'));
    if (active != null) tiles.appendChild(tile(active, 'active'));
    if (inactive != null) tiles.appendChild(tile(inactive, 'inactive'));
    box.appendChild(tiles);
    if (dem != null && rep != null && dem + rep > 0) {
      const base = total != null && total > dem + rep ? total : dem + rep;
      const oth = Math.max(0, base - dem - rep);
      const pct = (v) => ((v / base) * 100).toFixed(1);
      box.appendChild(el(`<div class="reg-split" title="party registration share">
        <span class="rs-seg rs-dem" style="flex:${dem}">D ${pct(dem)}%</span>
        ${oth ? `<span class="rs-seg rs-oth" style="flex:${oth}"></span>` : ''}
        <span class="rs-seg rs-rep" style="flex:${rep}">R ${pct(rep)}%</span>
      </div>`));
      box.appendChild(el(`<div class="dim" style="font-size:10px">DEM ${Number(dem).toLocaleString()} · REP ${Number(rep).toLocaleString()}${oth ? ` · other/unaffiliated ${Number(oth).toLocaleString()}` : ''}</div>`));
    } else {
      box.appendChild(el(`<div class="dim" style="font-size:10px">this state does not report registration by party — no split shown</div>`));
    }
    return box;
  }

  _analystButton(type, id, label) {
    const b = el(`<button class="primary">Ask the Analyst</button>`);
    b.addEventListener('click', () => this.bag.openAnalyst(type, id, label));
    return b;
  }

  /** Tabbed sub-nav: renders a tab bar + one container per tab into `body`
      and returns {sections}. Panels render ONCE into their section; tab
      clicks only toggle visibility. Last tab per page TYPE is remembered in
      localStorage. Each section starts with a skeleton that `_fill` clears. */
  _tabs(body, pageType, defs) {
    const storeKey = `pollgrid.tab.${pageType}`;
    let active = null;
    try { active = localStorage.getItem(storeKey); } catch (e) { /* noop */ }
    if (!defs.some((d) => d.key === active)) active = defs[0].key;
    const bar = el(`<div class="pane-tabs" role="tablist">${defs.map((d) =>
      `<button data-tab="${d.key}" role="tab" aria-selected="${d.key === active}" class="${d.key === active ? 'active' : ''}">${escapeHtml(d.label)}</button>`).join('')}</div>`);
    body.appendChild(bar);
    const sections = {};
    for (const d of defs) {
      const sec = el(`<div class="tab-section" role="tabpanel"></div>`);
      sec.hidden = d.key !== active;
      sec.appendChild(skeleton(3));
      body.appendChild(sec);
      sections[d.key] = sec;
    }
    bar.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-tab]');
      if (!b) return;
      for (const x of bar.querySelectorAll('button[data-tab]')) {
        x.classList.toggle('active', x === b);
        x.setAttribute('aria-selected', String(x === b));
      }
      for (const d of defs) sections[d.key].hidden = d.key !== b.dataset.tab;
      try { localStorage.setItem(storeKey, b.dataset.tab); } catch (e2) { /* noop */ }
    });
    return sections;
  }

  /** Append `elm` into a tab section, clearing its skeleton on first fill. */
  _fill(section, elm) {
    const sk = section.querySelector(':scope > .skel-group');
    if (sk) sk.remove();
    if (elm) section.appendChild(elm);
    return elm;
  }

  /** 'Backed by' — independent-expenditure/PAC backing rows from the
      backed_by array on race & candidate payloads. Support/oppose chips link
      to the org's ledger page. */
  _backedByPanel(rows) {
    const { root, body } = panel('Backed by');
    if (!rows || !rows.length) {
      body.appendChild(empty('No PAC or independent-expenditure backing recorded.',
        'rows appear as the FEC independent-expenditure sync lands'));
      return root;
    }
    for (const b of rows.slice(0, 20)) {
      const support = String(b.spend_type || '').toLowerCase() !== 'oppose';
      body.appendChild(el(`<div class="backed-row">
        <a href="#/lobby/${b.org_id}">${escapeHtml(b.org)}</a>
        ${b.sector ? `<span class="chip accent">${escapeHtml(b.sector)}</span>` : ''}
        <span class="chip ${support ? 'ok' : 'err'}">${icon(support ? 'support' : 'oppose')}${escapeHtml(b.spend_type || 'support')}</span>
        <span class="br-amt">$${Number(b.amount || 0).toLocaleString()}</span>
      </div>`));
    }
    body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">FEC independent expenditures via the influence ledger — <a href="#/lobbies">full directory →</a></div>`));
    return root;
  }

  /* ---------------- race page ---------------- */

  async _racePage(body, id) {
    const data = await this.bag.api.race(id);
    body.innerHTML = '';
    if (!data || !data.race) {
      body.appendChild(el(`<h2 class="pane-title">Race #${escapeHtml(String(id))}</h2>`));
      body.appendChild(empty('Race detail unavailable.', `GET /api/races/${id} failed — backend offline or unknown race`));
      body.appendChild(await this._pollsPanel({ race_id: id }, null));
      return;
    }
    const r = data.race;
    if (this.bag.setAnalystContext) this.bag.setAnalystContext('race', id, r.name || `race #${id}`);
    body.appendChild(el(`<h2 class="pane-title">${escapeHtml(r.name || 'Race #' + id)}</h2>`));
    body.appendChild(el(`<div class="pane-sub">${officeIcon(r.race_type, OFFICE_LABEL[r.race_type])}${escapeHtml(r.race_type || '')} · cycle ${escapeHtml(String(r.cycle_year || '?'))}
      · ${escapeHtml(r.phase || '')} ${r.status ? '· ' + escapeHtml(r.status) : ''}
      ${r.competitiveness ? `· <span class="chip accent">${escapeHtml(r.competitiveness)}</span>` : ''}</div>`));

    const btnRow = el(`<div class="row"></div>`);
    btnRow.appendChild(this._analystButton('race', id, r.name || `race #${id}`));
    const enBtn = el(`<button>Election Night Mode</button>`);
    enBtn.addEventListener('click', () => this.bag.activateElectionNight(id));
    btnRow.appendChild(enBtn);
    if (r.state_fips) {
      const fly = el(`<button>Fly to</button>`);
      fly.addEventListener('click', () => this.bag.flyToState(r.state_fips));
      btnRow.appendChild(fly);
    }
    body.appendChild(btnRow);

    // tabbed sub-nav — panels render once into their tab, clicks only toggle
    const tabs = this._tabs(body, 'race', [
      { key: 'overview', label: 'Overview' },
      { key: 'polls', label: 'Polls' },
      { key: 'factors', label: 'Factors' },
      { key: 'finance', label: 'Finance' },
      { key: 'coverage', label: 'Coverage' },
    ]);

    // candidates
    if (data.candidates && data.candidates.length) {
      const { root, body: cb } = panel('Candidates');
      for (const c of data.candidates) {
        cb.appendChild(el(`<div class="kv">
          <span class="k"><a href="#/candidate/${c.id}">${escapeHtml(c.name)}</a>
            <span class="chip ${partyChipClass(c.party_code)}">${escapeHtml(c.party_code || '?')}</span>
            ${c.is_incumbent ? '<span class="chip">incumbent</span>' : ''}</span>
          <span class="v">${c.ideology_score != null ? 'ideo ' + Number(c.ideology_score).toFixed(2) : ''}</span></div>`));
      }
      this._fill(tabs.overview, root);
    }

    // average + trend sparkline (trend rebuilt from the race's own poll toplines)
    {
      const { root, body: ab } = panel('Polling average');
      if (data.average && data.average.parties) {
        for (const [p, v] of Object.entries(data.average.parties)) {
          const row = el(`<div class="bar-row"><span class="bar-label"><span class="chip ${partyChipClass(p)}">${escapeHtml(p)}</span></span>
            <span class="bar-track"><span class="bar-fill ${partyChipClass(p)}" style="width:${Math.min(100, v)}%"></span></span>
            <span class="bar-val"></span></div>`);
          ab.appendChild(row);
          animateNumber(row.querySelector('.bar-val'), 0, Number(v), 650, (x) => x.toFixed(1) + '%');
        }
        ab.appendChild(el(`<div class="dim" style="font-size:10px">as of ${escapeHtml(data.average.as_of || '?')} · ${data.average.n_polls ?? '?'} polls · recency/house/sample-weighted, zero LLM</div>`));
        const pollsRes = await this.bag.api.polls({ race_id: id, limit: 40 });
        const rows = (pollsRes && pollsRes.rows) || [];
        const margins = rows
          .filter((p) => p.results && p.results.DEM != null && p.results.REP != null)
          .sort((a, b) => String(a.field_end).localeCompare(String(b.field_end)))
          .map((p) => p.results.DEM - p.results.REP);
        if (margins.length > 1) {
          ab.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:6px">D−R margin, poll by poll:</div>`));
          ab.appendChild(sparkline(margins));
        }
      } else {
        ab.appendChild(empty('No average yet for this race.', 'average appears once enough polls are ingested'));
      }
      this._fill(tabs.overview, root);
    }

    // fundamentals
    {
      const { root, body: fb } = panel('Fundamentals');
      const f = data.fundamentals;
      if (f && f.components) {
        for (const [k, v] of Object.entries(f.components)) {
          const w = Math.min(100, Math.abs(Number(v)) * 100);
          fb.appendChild(el(`<div class="bar-row"><span class="bar-label">${escapeHtml(k)}</span>
            <span class="bar-track"><span class="bar-fill ${v >= 0 ? 'dem' : 'rep'}" style="width:${w}%"></span></span>
            <span class="bar-val">${Number(v).toFixed(2)}</span></div>`));
        }
        fb.appendChild(el(`<div class="dim" style="font-size:10px">dem_score ${f.dem_score != null ? Number(f.dem_score).toFixed(3) : '?'} · as of ${escapeHtml(f.as_of || '?')}</div>`));
      } else fb.appendChild(empty('No fundamentals snapshot.', 'deterministic composite — appears once inputs sync'));
      this._fill(tabs.overview, root);
    }

    // forecast card — honesty: show gate_reason when not visible
    {
      const { root, body: fb } = panel('Forecast');
      const fc = data.forecast;
      if (fc && fc.visible) {
        fb.appendChild(el(`<div class="row" style="justify-content:space-between">
          <span style="color:var(--dem);font-family:var(--mono);font-size:20px">${(fc.dem_prob * 100).toFixed(0)}% D</span>
          <span style="color:var(--rep);font-family:var(--mono);font-size:20px">${(fc.rep_prob * 100).toFixed(0)}% R</span></div>`));
        fb.appendChild(el(`<div class="bar-track mt"><span class="bar-fill dem" style="width:${fc.dem_prob * 100}%"></span></div>`));
        fb.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">model: ${escapeHtml(fc.model || '?')} · earned via Brier backtest — see <a href="#/scorecard">scorecard</a></div>`));
        const ens = await this.bag.api.ensemble(id);
        if (ens) {
          fb.appendChild(el(`<div class="kv"><span class="k">quantitative-only</span><span class="v">${ens.quantitative ? (ens.quantitative.dem_prob * 100).toFixed(1) + '% D' : '—'}</span></div>`));
          fb.appendChild(el(`<div class="kv"><span class="k">qualitative-augmented</span><span class="v">${ens.ensemble ? (ens.ensemble.dem_prob * 100).toFixed(1) + '% D' : 'not earned yet'}</span></div>`));
          fb.appendChild(el(`<div class="dim" style="font-size:10px">live model: ${escapeHtml(ens.live_model || '?')}</div>`));
        }
      } else if (fc) {
        fb.appendChild(empty('Forecast gated off for this race type.',
          fc.gate_reason || 'has not cleared the Brier-score backtest gate'));
      } else {
        fb.appendChild(empty('No forecast for this race.', 'forecast route unavailable'));
      }
      this._fill(tabs.overview, root);
    }

    // corroboration badge + narrative
    {
      const { root, body: cb } = panel('Signals');
      if (data.corroboration && data.corroboration.badge) {
        cb.appendChild(el(`<span class="chip ok" title="poll direction corroborated by independently-sourced non-poll signals">✓ corroborated</span>`));
        for (const s of data.corroboration.signals || []) {
          const arrow = s.direction > 0 ? ' ↑D' : (s.direction < 0 ? ' ↑R' : '');
          const count = s.count != null ? ` ×${s.count}` : '';
          cb.appendChild(el(`<span class="chip">${escapeHtml(String(s.channel || s))}${arrow}${count}</span>`));
        }
      } else {
        cb.appendChild(el(`<span class="chip" title="no independent corroboration yet">uncorroborated</span>`));
      }
      const n = data.narrative;
      if (n) {
        cb.appendChild(el(`<div class="mt" style="font-size:12.5px">
          <b>What changed:</b> ${escapeHtml(n.what_changed || '—')}<br>
          <b>Why it might have:</b> ${escapeHtml(n.why_it_might_have_changed || '—')}<br>
          <b>What to watch:</b> ${escapeHtml(n.what_to_watch || '—')}
          <div class="dim" style="font-size:10px;margin-top:4px">confidence: ${escapeHtml(String(n.confidence ?? '?'))} · generated by ${escapeHtml(n.generated_by || '?')}</div></div>`));
      }
      this._fill(tabs.overview, root);
    }

    // counterfactual launcher
    tabs.overview.appendChild(this._counterfactualPanel(id, data.candidates || [], r.cycle_year));

    // finance — PAC / independent-expenditure backing
    this._fill(tabs.finance, this._backedByPanel(data.backed_by));

    // Qualitative Factor Scorecard
    this._fill(tabs.factors, await this._factorsPanel(id));

    // polls
    this._fill(tabs.polls, await this._pollsPanel({ race_id: id }, null, 'Polls — this race'));

    // coverage: framing matrix mini-heatmap + articles
    this._fill(tabs.coverage, await this._framingPanel(id));
    tabs.coverage.appendChild(await this._articlesPanel('race', id));
  }

  async _factorsPanel(raceId) {
    const { root, body } = panel('Qualitative Factor Scorecard');
    const f = await this.bag.api.factors(raceId);
    if (!f || !f.factors || !f.factors.length) {
      body.appendChild(empty('No factor scores for this race.',
        `GET /api/factors/${raceId} empty — degrades to quantitative-only, never guesses`));
      return root;
    }
    const t = el(`<table class="grid"><thead><tr><th>Factor</th><th>Family</th><th>Method</th><th>Score</th><th>Cited</th></tr></thead><tbody></tbody></table>`);
    const tb = t.querySelector('tbody');
    for (const fac of f.factors) {
      const methodChip = fac.method === 'deterministic' ? 'ok' : fac.method === 'llm_rubric' ? 'warn' : '';
      const score = Number(fac.score) || 0;
      const w = Math.min(100, Math.abs(score) * 50);
      const tr = el(`<tr>
        <td title="${escapeHtml(fac.rationale || '')}">${escapeHtml(fac.name || fac.key)}</td>
        <td class="dim" style="font-size:10px">${escapeHtml(fac.family || '')}</td>
        <td><span class="chip ${methodChip}">${escapeHtml(fac.method || '?')}</span></td>
        <td><div class="bar-track" style="width:70px"><span class="bar-fill ${score >= 0 ? 'dem' : 'rep'}" style="width:${w}%"></span></div></td>
        <td class="mono" style="font-size:10px">${(fac.citations || []).map((c) => `<span class="cite-chip" title="fact #${escapeHtml(String(c))}">${escapeHtml(String(c))}</span>`).join('') || '—'}</td>
      </tr>`);
      tb.appendChild(tr);
    }
    body.appendChild(t);
    body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">as of ${escapeHtml(f.as_of || '?')} · scored against a fixed rubric, cited, cached — never open-ended; neutral_fallback = no LLM was reachable</div>`));
    return root;
  }

  async _framingPanel(raceId) {
    const { root, body } = panel('Media framing');
    const fr = await this.bag.api.framing(raceId);
    if (!fr || !fr.matrix || !fr.matrix.length) {
      body.appendChild(empty('No framing matrix.', `GET /api/races/${raceId}/framing empty or unavailable`));
      return root;
    }
    const outlets = [...new Set(fr.matrix.map((m) => m.outlet))].slice(0, 8);
    const topics = [...new Set(fr.matrix.map((m) => m.topic))].slice(0, 6);
    const cell = (o, t) => fr.matrix.find((m) => m.outlet === o && m.topic === t);
    const table = el(`<div style="overflow-x:auto"><table class="heatmap"><thead><tr><th></th>${topics.map((t) => `<th>${escapeHtml(t)}</th>`).join('')}</tr></thead><tbody></tbody></table></div>`);
    const tb = table.querySelector('tbody');
    for (const o of outlets) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<th>${escapeHtml(o)}</th>` + topics.map((t) => {
        const c = cell(o, t);
        if (!c) return '<td></td>';
        const dir = String(c.framing || '').toLowerCase();
        const color = dir.includes('dem') || dir.includes('favor_d') ? 'var(--dem)' : dir.includes('rep') || dir.includes('favor_r') ? 'var(--rep)' : 'var(--other)';
        return `<td style="background:color-mix(in srgb, ${color} 45%, transparent)" title="${escapeHtml(c.outlet)} (${escapeHtml(c.leaning || '?')}) on ${escapeHtml(t)}: ${escapeHtml(c.framing || '')}">·</td>`;
      }).join('');
      tb.appendChild(tr);
    }
    body.appendChild(table);
    if (fr.ad_spend && fr.ad_spend.length) {
      const max = Math.max(...fr.ad_spend.map((a) => a.amount || 0)) || 1;
      body.appendChild(el(`<div class="dim" style="font-size:11px;margin-top:8px">Ad spend by sponsor</div>`));
      for (const a of fr.ad_spend.slice(0, 8)) {
        body.appendChild(el(`<div class="bar-row"><span class="bar-label">${escapeHtml(a.sponsor)} <span class="dim">(${escapeHtml(a.medium || '?')})</span></span>
          <span class="bar-track"><span class="bar-fill" style="width:${(a.amount / max) * 100}%"></span></span>
          <span class="bar-val">$${Number(a.amount).toLocaleString()}</span></div>`));
      }
    }
    return root;
  }

  _counterfactualPanel(raceId, candidates, cycle) {
    const { root, body } = panel('Counterfactuals');
    const row = el(`<div class="row"></div>`);
    for (const c of candidates) {
      const b = el(`<button>What if ${escapeHtml(c.name.split(' ').pop())} drops out?</button>`);
      b.addEventListener('click', () => this._runCounterfactual(body, raceId, `dropout:${c.id}`));
      row.appendChild(b);
    }
    for (const cy of [2020, 2016]) {
      const b = el(`<button>Turnout like ${cy}?</button>`);
      b.addEventListener('click', () => this._runCounterfactual(body, raceId, `turnout:${cy}`));
      row.appendChild(b);
    }
    body.appendChild(row);
    body.appendChild(el(`<div class="cf-out"></div>`));
    return root;
  }

  async _runCounterfactual(body, raceId, scenario) {
    const out = body.querySelector('.cf-out');
    out.innerHTML = '<div class="dim">branching…</div>';
    const cf = await this.bag.api.counterfactual(raceId, scenario);
    out.innerHTML = '';
    if (!cf || !cf.branches) {
      out.appendChild(empty('Counterfactual engine unavailable.', 'GET /api/counterfactual failed — backend offline'));
      return;
    }
    for (const b of cf.branches) {
      out.appendChild(el(`<div class="panel" style="margin:8px 0"><div class="panel-body">
        <b>${escapeHtml(b.label)}</b>
        <div class="mono" style="font-size:11px">${Object.entries(b.probs || {}).map(([k, v]) => `${escapeHtml(k)} ${(v * 100).toFixed(0)}%`).join(' · ')}</div>
        ${b.narrative ? `<div style="font-size:12px;margin-top:4px">${escapeHtml(b.narrative)}</div>` : ''}
        ${(b.precedents || []).length ? `<div class="dim" style="font-size:10px;margin-top:4px">precedents: ${b.precedents.map((p) => `${p.cycle_year} ${escapeHtml(p.state || '')} ${escapeHtml(p.office || '')}: ${escapeHtml(p.winner_party || '?')} by ${p.margin_pct != null ? Number(p.margin_pct).toFixed(1) : '?'} pts`).join('; ')}</div>` : ''}
      </div></div>`));
    }
    out.appendChild(el(`<div class="dim" style="font-size:10px">probabilities honestly non-summing · generated by ${escapeHtml(cf.generated_by || '?')}</div>`));
  }

  /* ---------------- story detail (#/story/{id}) ---------------- */

  async _storyPage(body, id) {
    const data = await this.bag.api.story(id);
    body.innerHTML = '';
    if (!data || !data.story) {
      body.appendChild(el(`<h2 class="pane-title">Story #${escapeHtml(String(id))}</h2>`));
      body.appendChild(empty('Story unavailable.',
        `GET /api/stories/${id} failed — unknown story id, or the backend is offline`));
      return;
    }
    const s = data.story;
    body.appendChild(el(`<h2 class="pane-title">${escapeHtml(s.headline || '(untitled story)')}</h2>`));

    const meta = el(`<div class="pane-sub row" style="row-gap:4px"></div>`);
    if (s.category) meta.appendChild(el(`<span class="chip accent">${escapeHtml(s.category)}</span>`));
    if (s.race_id) {
      const raceLabel = (data.race && data.race.name) || `race #${s.race_id}`;
      meta.appendChild(el(`<a href="#/race/${s.race_id}"><span class="chip dem" style="cursor:pointer" title="open the related race">${escapeHtml(raceLabel)}</span></a>`));
    }
    if (s.state_fips) {
      const fips = String(s.state_fips).padStart(2, '0');
      const name = this.bag.statesByFips[fips]?.name || `state ${fips}`;
      meta.appendChild(el(`<a href="#/state/${fips}"><span class="chip" style="cursor:pointer">${escapeHtml(name)}</span></a>`));
    }
    if (s.is_synthetic) meta.appendChild(el(`<span class="chip warn synth-chip" title="synthetic demo row — remove with scripts/purge_synthetic.py">SYNTH</span>`));
    if (s.score != null) meta.appendChild(el(`<span class="dim mono" style="font-size:10px">score ${Number(s.score).toFixed(2)}</span>`));
    if (s.created_at) meta.appendChild(el(`<span class="dim mono" style="font-size:10px">${escapeHtml(String(s.created_at).slice(0, 16).replace('T', ' '))}</span>`));
    body.appendChild(meta);

    const { root, body: fb } = panel('Fact timeline');
    const facts = data.facts || [];
    if (!facts.length) {
      fb.appendChild(empty('No extracted facts attached to this story yet.',
        'facts appear as the extraction pipeline processes the underlying articles'));
    } else {
      for (const f of facts) {
        fb.appendChild(el(`
          <div class="fact-row">
            <div class="fact-summary">${escapeHtml(f.summary || '(no summary)')}</div>
            <div class="fact-meta">
              ${f.category ? `<span class="chip">${escapeHtml(f.category)}</span>` : ''}
              <span class="mono dim">${escapeHtml(String(f.occurred_at || f.created_at || '?').slice(0, 16).replace('T', ' '))}</span>
              ${f.outlet ? `<span class="dim">${escapeHtml(f.outlet)}</span>` : ''}
              ${f.reliability_tier != null ? `<span class="chip" title="source reliability tier">tier ${escapeHtml(String(f.reliability_tier))}</span>` : ''}
              ${f.url ? `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">source ↗</a>` : ''}
            </div>
          </div>`));
      }
      fb.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:6px">${facts.length} fact(s), newest first — every fact traces to its raw source item</div>`));
    }
    body.appendChild(root);
  }

  /* ---------------- geography pages ---------------- */

  async _statePage(body, fips) {
    const local = this.bag.statesByFips[fips];
    const [geoStates, races, districts] = await Promise.all([
      this.bag.api.geoStates(),
      this.bag.api.races({ state: fips }),
      this.bag.api.geoDistricts(fips),
    ]);
    const meta = (geoStates || []).find((s) => s.fips_code === fips);
    body.innerHTML = '';
    if (this.bag.setAnalystContext) this.bag.setAnalystContext('state', fips, meta?.name || local?.name || `state ${fips}`);

    // header: state flag (onerror → hidden, never a broken-image glyph) + name
    const h = el(`<h2 class="pane-title">${escapeHtml(meta?.name || local?.name || 'State ' + fips)}</h2>`);
    if (meta?.flag_url) {
      const flag = document.createElement('img');
      flag.className = 'state-flag';
      flag.alt = `${meta.name} flag`;
      // hide (not remove) on error — stays in the DOM, never a broken glyph
      flag.addEventListener('error', () => { flag.style.display = 'none'; });
      flag.src = meta.flag_url;
      h.prepend(flag);
    }
    body.appendChild(h);
    body.appendChild(el(`<div class="pane-sub">FIPS ${escapeHtml(fips)}
      ${meta ? ` · ${escapeHtml(meta.usps_code)}
        ${meta.is_territory ? '· <span class="chip">territory — no voting House seat</span>' : ''}`
      : ' · <span class="dim">live elector data unavailable (backend offline)</span>'}</div>`));

    // at-a-glance strip — only tiles whose data actually exists
    const glance = el(`<div class="glance-strip"></div>`);
    const tile = (val, lab, title = '') =>
      el(`<div class="glance-tile" ${title ? `title="${escapeHtml(title)}"` : ''}><div class="g-val">${escapeHtml(String(val))}</div><div class="g-lab">${escapeHtml(lab)}</div></div>`);
    if (meta?.electoral_votes != null) glance.appendChild(tile(meta.electoral_votes, 'electoral votes'));
    if (meta?.elector_method) glance.appendChild(tile(meta.elector_method === 'congressional_district' ? 'by CD' : meta.elector_method, 'elector method', 'how this state awards electors'));
    const houseSeats = (districts || []).filter((d) => d.is_voting).length;
    if (houseSeats) glance.appendChild(tile(houseSeats, 'house seats', 'voting congressional districts, current boundaries'));
    const senateClasses = [...new Set((races || [])
      .filter((r) => r.race_type === 'senate' && /^class_\d$/.test(String(r.seat || '')))
      .map((r) => String(r.seat).replace('class_', '')))].sort();
    if (senateClasses.length) glance.appendChild(tile(senateClasses.join(' & '), 'senate class up', 'senate seat class(es) with a tracked race'));
    if (glance.children.length) body.appendChild(glance);

    const btns = el(`<div class="row"></div>`);
    btns.appendChild(this._analystButton('state', fips, meta?.name || local?.name || fips));
    const fly = el(`<button>Fly to</button>`);
    fly.addEventListener('click', () => this.bag.flyToState(fips));
    btns.appendChild(fly);
    body.appendChild(btns);

    const tabs = this._tabs(body, 'state', [
      { key: 'overview', label: 'Overview' },
      { key: 'elections', label: 'Elections' },
      { key: 'demographics', label: 'Demographics' },
      { key: 'delegation', label: 'Delegation' },
      { key: 'coverage', label: 'Coverage' },
    ]);

    // overview: races in this state
    const { root: rp, body: rb } = panel('Races');
    if (races && races.length) {
      for (const r of races.slice(0, 15)) {
        rb.appendChild(el(`<div class="kv"><span class="k">${officeIcon(r.race_type)}<a href="#/race/${r.id}">${escapeHtml(r.name)}</a></span>
          <span class="v">${r.leader_party ? `<span class="chip ${partyChipClass(r.leader_party)}">${escapeHtml(r.leader_party)}${r.leader_margin != null ? ' +' + Number(r.leader_margin).toFixed(1) : ''}</span>` : ''}</span></div>`));
      }
    } else rb.appendChild(empty('No tracked races here.', `GET /api/races?state=${fips} empty or unavailable`));
    this._fill(tabs.overview, rp);

    // elections: history + statewide polls
    this._fill(tabs.elections, await this._historyPanel('state', fips));
    tabs.elections.appendChild(await this._pollsPanel({ state: fips }, 'Statewide-scope polls (Senate, Governor, President-in-state).'));

    // demographics (+ voter-registration block when those rows exist)
    this._fill(tabs.demographics, await this._demographicsPanel('state', fips, { registration: true }));

    // delegation: portrait grid + party-split donut
    this._fill(tabs.delegation, await this._delegationPanel(fips));

    // coverage
    this._fill(tabs.coverage, this._geoArticlesNote());
  }

  /** Delegation tab — portrait grid of this state's officeholders/candidates
      (governor / senate / house), initials avatars when a portrait is missing
      or unloadable, plus an inline-SVG party-split donut. */
  async _delegationPanel(fips) {
    const { root, body } = panel('Delegation & candidates');
    const offices = ['governor', 'senate', 'house'];
    const results = await Promise.all(offices.map((o) =>
      this.bag.api.candidates({ state: fips, office: o, limit: 60 })));
    const people = [];
    const seen = new Set();
    results.forEach((rows, i) => {
      for (const c of rows || []) {
        if (seen.has(c.id)) continue;
        seen.add(c.id);
        people.push({ ...c, office: c.office || offices[i] });
      }
    });
    if (!people.length) {
      body.appendChild(empty('No tracked officeholders or candidates for this state.',
        `GET /api/candidates?state=${fips}&office=governor|senate|house empty or unavailable`));
      return root;
    }
    people.sort((a, b) => offices.indexOf(a.office) - offices.indexOf(b.office) || String(a.name).localeCompare(String(b.name)));

    // party-split donut
    const counts = { DEM: 0, REP: 0, other: 0 };
    for (const p of people) counts[p.party_code === 'DEM' || p.party_code === 'REP' ? p.party_code : 'other']++;
    const donutRow = el(`<div class="row" style="margin-bottom:10px"></div>`);
    donutRow.appendChild(donutSvg([
      { label: 'Democratic', value: counts.DEM, colorVar: 'var(--dem)' },
      { label: 'Republican', value: counts.REP, colorVar: 'var(--rep)' },
      { label: 'Other / unaffiliated', value: counts.other, colorVar: 'var(--other)' },
    ]));
    donutRow.appendChild(el(`<div style="font-size:11px">
      <div><span class="chip dem">DEM ${counts.DEM}</span></div>
      <div class="mt" style="margin-top:4px"><span class="chip rep">REP ${counts.REP}</span></div>
      ${counts.other ? `<div style="margin-top:4px"><span class="chip other">OTHER ${counts.other}</span></div>` : ''}
    </div>`));
    body.appendChild(donutRow);

    const grid = el(`<div class="delegation-grid"></div>`);
    for (const p of people.slice(0, 48)) {
      const card = el(`<a class="delegate-card" href="#/candidate/${p.id}"></a>`);
      card.appendChild(avatar(p.name, p.party_code, p.portrait_url));
      card.appendChild(el(`<span class="delegate-name">${escapeHtml(p.name)}</span>`));
      card.appendChild(el(`<span class="delegate-office">${officeIcon(p.office)}${escapeHtml(OFFICE_LABEL[p.office] || p.office || '')} <span class="chip ${partyChipClass(p.party_code)}">${escapeHtml(p.party_code || '?')}</span></span>`));
      grid.appendChild(card);
    }
    body.appendChild(grid);
    if (people.length > 48) body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:6px">${people.length - 48} more not shown</div>`));
    body.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:6px">tracked candidates & officeholders from filing records — portraits load from cited sources, initials otherwise</div>`));
    return root;
  }

  async _countyPage(body, geoid) {
    const stateFips = geoid.slice(0, 2);
    const stateName = this.bag.statesByFips[stateFips]?.name || `state ${stateFips}`;
    const counties = await this.bag.api.geoCounties(stateFips);
    const meta = (counties || []).find((c) => c.geoid === geoid);
    const localName = this.bag.countyName ? this.bag.countyName(geoid) : null;
    body.innerHTML = '';
    if (this.bag.setAnalystContext) this.bag.setAnalystContext('county_equivalent', geoid, meta?.name || localName || `county ${geoid}`);
    body.appendChild(el(`<h2 class="pane-title">${escapeHtml(meta?.name || localName || 'County ' + geoid)}</h2>`));
    body.appendChild(el(`<div class="pane-sub">GEOID ${escapeHtml(geoid)} · ${escapeHtml(stateName)}
      ${meta?.type ? `· <span class="chip" title="county-equivalent type — never hardcoded as 'county'">${escapeHtml(meta.type)}</span>` : ''}</div>`));

    const btns = el(`<div class="row"></div>`);
    btns.appendChild(this._analystButton('county_equivalent', geoid, meta?.name || localName || geoid));
    const fly = el(`<button>Fly to</button>`);
    fly.addEventListener('click', () => this.bag.flyToCounty(geoid));
    btns.appendChild(fly);
    const up = el(`<button>↑ ${escapeHtml(stateName)}</button>`);
    up.addEventListener('click', () => this.bag.navigate(`#/state/${stateFips}`));
    btns.appendChild(up);
    body.appendChild(btns);

    // county-scope labeling rule: state/district polls "covering" this county
    body.appendChild(await this._pollsPanel(
      { state: stateFips },
      `Pollsters essentially never field county-level surveys. These are ${stateName} state/district-scope polls COVERING this county — labeled at that scope, never fabricated county polls.`,
      'Polls covering this county'));
    body.appendChild(this._geoArticlesNote());
    body.appendChild(await this._historyPanel('county_equivalent', geoid));
    body.appendChild(await this._demographicsPanel('county_equivalent', geoid));
  }

  async _districtPage(body, id) {
    body.innerHTML = '';
    body.appendChild(el(`<h2 class="pane-title">District ${escapeHtml(String(id))}</h2>`));
    const fair = await this.bag.api.fairness(id);
    if (fair) {
      const { root, body: fb } = panel('Plan fairness');
      fb.appendChild(el(`<div class="kv"><span class="k">efficiency gap</span><span class="v">${fair.efficiency_gap != null ? (fair.efficiency_gap * 100).toFixed(1) + '%' : '—'}</span></div>`));
      fb.appendChild(el(`<div class="kv"><span class="k">mean–median</span><span class="v">${fair.mean_median != null ? (fair.mean_median * 100).toFixed(1) + '%' : '—'}</span></div>`));
      fb.appendChild(el(`<div class="kv"><span class="k">districts in plan</span><span class="v">${fair.n_districts ?? '—'}</span></div>`));
      body.appendChild(root);
    } else {
      body.appendChild(empty('District fairness data unavailable.', `GET /api/districts/${id}/fairness failed`));
    }
    body.appendChild(await this._pollsPanel({}, 'District-race polls appear here once district data is live.'));
    body.appendChild(this._geoArticlesNote());
    body.appendChild(await this._historyPanel('congressional_district', id));
    body.appendChild(await this._demographicsPanel('congressional_district', id));
  }

  /* ---------------- candidate & party ---------------- */

  async _candidatePage(body, id) {
    const d = await this.bag.api.candidate(id);
    body.innerHTML = '';
    if (!d || !d.candidate) {
      body.appendChild(el(`<h2 class="pane-title">Candidate #${escapeHtml(String(id))}</h2>`));
      body.appendChild(empty('Candidate dossier unavailable.', `GET /api/candidates/${id} failed — backend offline or unknown id`));
      return;
    }
    const c = d.candidate;
    if (this.bag.setAnalystContext) this.bag.setAnalystContext('candidate', id, c.name || `candidate #${id}`);
    const head = el(`<div class="row" style="align-items:center;gap:10px;flex-wrap:nowrap"></div>`);
    head.appendChild(avatar(c.name, c.party_code, c.portrait_url, true));
    head.appendChild(el(`<div><h2 class="pane-title" style="margin-bottom:2px">${escapeHtml(c.name)}</h2>
      <div class="pane-sub" style="margin-bottom:0"><span class="chip ${partyChipClass(c.party_code)}">${escapeHtml(c.party_code || '?')}</span>
      ${c.office ? officeIcon(c.office) + escapeHtml(c.office) : ''} ${c.state_fips ? '· ' + escapeHtml(this.bag.statesByFips[c.state_fips]?.name || c.state_fips) : ''}
      ${c.curated ? '<span class="chip ok" title="hand-seeded curated floor with cited sources">curated</span>' : '<span class="chip" title="auto-seeded from filing records; thickens as sync and AI-fill reach it">stub</span>'}</div></div>`));
    body.appendChild(head);
    body.appendChild(this._analystButton('candidate', id, c.name));

    const tabs = this._tabs(body, 'candidate', [
      { key: 'overview', label: 'Overview' },
      { key: 'races', label: 'Races' },
      { key: 'finance', label: 'Finance' },
      { key: 'coverage', label: 'Coverage' },
    ]);

    if (c.bio || c.positions_summary) {
      const { root, body: bb } = panel('Dossier');
      if (c.bio) bb.appendChild(el(`<p style="font-size:12.5px">${escapeHtml(c.bio)}</p>`));
      if (c.positions_summary) bb.appendChild(el(`<p style="font-size:12.5px">${escapeHtml(c.positions_summary)}</p>`));
      if (c.citation) bb.appendChild(el(`<div class="dim" style="font-size:10px">source: ${escapeHtml(c.citation)}</div>`));
      this._fill(tabs.overview, root);
    }

    if (d.stances && d.stances.length) {
      const { root, body: sb } = panel('Topic stances');
      for (const s of d.stances.slice(0, 12)) {
        sb.appendChild(el(`<div class="kv"><span class="k">${escapeHtml(s.topic)}</span>
          <span class="v" style="font-family:var(--sans);font-size:12px">${escapeHtml(s.stance)} <span class="chip">${escapeHtml(s.method || '?')}</span></span></div>`));
      }
      this._fill(tabs.overview, root);
    }
    if (!tabs.overview.querySelector('.panel')) {
      this._fill(tabs.overview, empty('Sparse dossier — thickens as sync and AI-fill reach it.'));
    }

    const { root: rr, body: rb } = panel('Races');
    if (d.races && d.races.length) {
      for (const r of d.races.slice(0, 12)) {
        rb.appendChild(el(`<div class="kv"><span class="k"><a href="#/race/${r.id}">${escapeHtml(r.name || 'race #' + r.id)}</a></span>
          <span class="v">${escapeHtml(String(r.cycle_year || ''))}</span></div>`));
      }
    } else rb.appendChild(empty('No tracked races for this candidate.'));
    this._fill(tabs.races, rr);

    const { root: ip, body: ib } = panel('Finance & ideology');
    if (d.finance) {
      ib.appendChild(el(`<div class="kv"><span class="k">total receipts</span><span class="v">$${Number(d.finance.total_receipts || 0).toLocaleString()}</span></div>`));
      ib.appendChild(el(`<div class="dim" style="font-size:10px">FEC, as of ${escapeHtml(d.finance.as_of || '?')}</div>`));
    } else ib.appendChild(empty('No FEC finance summary.', 'finance sync has not reached this candidate'));
    if (d.ideology) {
      ib.appendChild(el(`<div class="kv"><span class="k">ideology score <span class="chip" title="deterministic proxy from roll calls, donors, positions — never an LLM guess">deterministic</span></span>
        <span class="v">${Number(d.ideology.score).toFixed(2)}</span></div>`));
    }
    this._fill(tabs.finance, ip);
    tabs.finance.appendChild(this._backedByPanel(d.backed_by));

    this._fill(tabs.coverage, await this._articlesPanel('candidate', id));
  }

  /* ---------------- lobby / influence org (#/lobby/{id}) ---------------- */

  async _lobbyPage(body, id) {
    const d = await this.bag.api.lobby(id);
    body.innerHTML = '';
    if (!d || !d.org) {
      body.appendChild(el(`<h2 class="pane-title">Organization #${escapeHtml(String(id))}</h2>`));
      body.appendChild(empty('Influence-ledger entry unavailable.',
        `GET /api/lobbies/${id} failed — backend offline or unknown organization`));
      body.appendChild(el(`<a href="#/lobbies" style="font-size:11px">← full influence ledger</a>`));
      return;
    }
    const o = d.org;
    body.appendChild(el(`<h2 class="pane-title">${icon('org')} ${escapeHtml(o.name)}</h2>`));
    body.appendChild(el(`<div class="pane-sub">
      ${o.sector ? `<span class="chip accent">${escapeHtml(o.sector)}</span>` : ''}
      ${o.org_type ? `<span class="chip">${escapeHtml(o.org_type)}</span>` : ''}
      ${o.fec_committee_id ? `<span class="chip" title="FEC committee id">${escapeHtml(o.fec_committee_id)}</span>` : ''}
      · <a href="#/lobbies">influence ledger →</a></div>`));

    // lobbying disclosures (LDA)
    {
      const { root, body: db_ } = panel('Lobbying disclosures');
      if (d.disclosures && d.disclosures.length) {
        const t = el(`<div style="overflow-x:auto"><table class="grid"><thead><tr>
          <th>Period</th><th>Client</th><th>Issues</th><th class="num">Amount</th><th></th>
        </tr></thead><tbody></tbody></table></div>`);
        const tb = t.querySelector('tbody');
        for (const r of d.disclosures.slice(0, 40)) {
          tb.appendChild(el(`<tr>
            <td class="mono" style="font-size:11px">${escapeHtml(r.period || '?')}</td>
            <td>${escapeHtml(r.client || '—')}</td>
            <td class="mono" style="font-size:10px">${escapeHtml(r.issue_codes || '—')}</td>
            <td class="num">$${Number(r.amount || 0).toLocaleString()}</td>
            <td>${r.source_url ? `<a href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener">filing ↗</a>` : ''}</td>
          </tr>`));
        }
        db_.appendChild(t);
        db_.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">${d.disclosures.length} LDA filing(s), newest first</div>`));
      } else db_.appendChild(empty('No lobbying disclosures on file.', 'LDA sync has not reached this org, or it only spends via its PAC'));
      body.appendChild(root);
    }

    // independent expenditures by candidate
    {
      const { root, body: sb } = panel('Spend by candidate');
      if (d.spend && d.spend.length) {
        const t = el(`<div style="overflow-x:auto"><table class="grid"><thead><tr>
          <th>Candidate</th><th>Race</th><th>Type</th><th class="num">Amount</th><th class="num">Cycle</th>
        </tr></thead><tbody></tbody></table></div>`);
        const tb = t.querySelector('tbody');
        for (const s of d.spend.slice(0, 40)) {
          const support = String(s.spend_type || '').toLowerCase() !== 'oppose';
          tb.appendChild(el(`<tr>
            <td>${s.candidate_id ? `<a href="#/candidate/${s.candidate_id}">${escapeHtml(s.candidate || '#' + s.candidate_id)}</a>` : escapeHtml(s.candidate || '—')}</td>
            <td>${s.race_id ? `<a href="#/race/${s.race_id}">${escapeHtml(s.race || '#' + s.race_id)}</a>` : escapeHtml(s.race || '—')}</td>
            <td><span class="chip ${support ? 'ok' : 'err'}">${icon(support ? 'support' : 'oppose')}<span class="spend-type">${escapeHtml(s.spend_type || 'support')}</span></span></td>
            <td class="num">$${Number(s.amount || 0).toLocaleString()}</td>
            <td class="num">${escapeHtml(String(s.cycle_year || '—'))}</td>
          </tr>`));
        }
        sb.appendChild(t);
      } else sb.appendChild(empty('No independent expenditures recorded.', 'FEC IE sync has not reached this org'));
      body.appendChild(root);
    }

    // endorsements — the honesty rule: a row renders ONLY with the org's own
    // announcement URL; anything unsourced is withheld, and we say how many.
    {
      const { root, body: eb } = panel('Endorsements');
      const all = d.endorsements || [];
      const sourced = all.filter((e) => e.source_url);
      if (sourced.length) {
        for (const e of sourced.slice(0, 30)) {
          eb.appendChild(el(`<div class="backed-row">${icon('endorse')}
            <span>${escapeHtml(e.candidate || '?')}${e.race ? ` <span class="dim">· ${escapeHtml(e.race)}</span>` : ''}</span>
            <span class="dim mono" style="font-size:10px">${escapeHtml(String(e.as_of || '').slice(0, 10))}</span>
            <a class="br-amt" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener">announcement ↗</a>
          </div>`));
        }
      } else {
        eb.appendChild(empty('No sourced endorsements.', 'an endorsement displays only with the org’s own announcement URL'));
      }
      if (all.length > sourced.length) {
        eb.appendChild(el(`<div class="dim" style="font-size:10px;margin-top:4px">${all.length - sourced.length} endorsement row(s) withheld — no primary announcement URL on file</div>`));
      }
      body.appendChild(root);
    }

    if (o.citation) {
      body.appendChild(el(`<div class="lobby-cite">citation: ${escapeHtml(o.citation)}</div>`));
    }
  }

  async _partyPage(body, id) {
    const d = await this.bag.api.party(id);
    body.innerHTML = '';
    if (!d) {
      body.appendChild(el(`<h2 class="pane-title">Party #${escapeHtml(String(id))}</h2>`));
      body.appendChild(empty('Party dossier unavailable.', `GET /api/parties/${id} failed — backend offline or unknown id`));
      return;
    }
    const p = d.party || d;
    if (this.bag.setAnalystContext) this.bag.setAnalystContext('party', id, p.name || `party #${id}`);
    body.appendChild(el(`<h2 class="pane-title">${escapeHtml(p.name || 'Party #' + id)}</h2>`));
    if (p.code) body.appendChild(el(`<div class="pane-sub"><span class="chip ${partyChipClass(p.code)}">${escapeHtml(p.code)}</span></div>`));
    body.appendChild(this._analystButton('party', id, p.name || `party #${id}`));
    const { root, body: pb } = panel('Dossier');
    let wrote = false;
    for (const [k, v] of Object.entries(p)) {
      if (['id', 'name', 'code'].includes(k) || v == null || typeof v === 'object') continue;
      pb.appendChild(el(`<div class="kv"><span class="k">${escapeHtml(k)}</span><span class="v" style="font-family:var(--sans);font-size:12px;text-align:right;max-width:230px">${escapeHtml(String(v))}</span></div>`));
      wrote = true;
    }
    if (!wrote) pb.appendChild(empty('Sparse dossier — thickens as sync and AI-fill reach it.'));
    body.appendChild(root);
    body.appendChild(await this._articlesPanel('party', id));
  }
}
