/* App.js — boot + wiring. Plain state object, a fixed bag of callback
   closures for DI, components as plain classes instantiated once and mutated
   directly. Hash router; WebSocket feed with REST fallback; three-tier map
   with one-shot detection + manual override + graceful step-down when a
   tier's constructor throws. Must open cleanly with every /api/* 404ing. */

import { Api } from './api.js';
import { FeedSocket } from './socket.js';
import { chooseTier, getOverride, setOverride } from './components/map/TierDetector.js';
import { Tier1Globe } from './components/map/Tier1Globe.js';
import { Tier2Map } from './components/map/Tier2Map.js';
import { Tier3List } from './components/map/Tier3List.js';
import { ALL_MODES, getMode } from './components/map/modes.js';
import { featureInfo, rampColor, themePalette, rgbCss } from './components/map/geometry.js';
import { SlidePane } from './components/SlidePane.js';
import { PollsWindow, escapeHtml } from './components/PollsWindow.js';
import { MapBuilder } from './components/MapBuilder.js';
import { Feed } from './components/Feed.js';
import { ElectionNight } from './components/ElectionNight.js';
import { Analyst } from './components/Analyst.js';
import { TimeScrubber } from './components/TimeScrubber.js';
import { CommandPalette } from './components/CommandPalette.js';
import { SoundEngine } from './components/SoundEngine.js';
import { Scorecard } from './components/Scorecard.js';
import { Lobbies } from './components/Lobbies.js';
import { Diagnostics } from './components/Diagnostics.js';
import { Settings } from './components/Settings.js';
import { composeSnapshot, downloadCanvas } from './components/Snapshot.js';

const THEME_KEY = 'pollgrid.theme';
const RACE_TYPE_KEY = 'pollgrid.race_type';
const RACE_TYPES = ['president', 'senate', 'house', 'governor'];

/* DC & the territories — no map geometry to click (except PR), so the HUD
   chip row and the command palette are their navigation affordance. */
const TERRITORIES = [
  { key: '11', usps: 'DC', name: 'District of Columbia' },
  { key: '72', usps: 'PR', name: 'Puerto Rico' },
  { key: '66', usps: 'GU', name: 'Guam' },
  { key: '78', usps: 'VI', name: 'U.S. Virgin Islands' },
  { key: '60', usps: 'AS', name: 'American Samoa' },
  { key: '69', usps: 'MP', name: 'Northern Mariana Islands' },
];

/* ---------------- state ---------------- */

const state = {
  theme: 'newsroom',
  tier: null,               // active renderer tier (1|2|3)
  tierSource: 'detected',
  asOf: null,               // 'YYYY-MM-DD' | null = LIVE
  mode: 'partisan_lean',    // thematic map mode
  raceType: 'senate',       // race_type for the value modes (HUD segmented control)
  mapTier: 'state',         // choropleth tier currently shown (LOD)
  selection: null,          // {type, id}
  electionNight: false,
  builderActive: false,
  status: null,             // /api/status
  socketUp: false,
  statesGeo: null,
  countiesGeo: null,
  districtsAvailable: false,
  districtActive: false,    // House race-type pins the district overlay + choropleth tier
};

const api = new Api(() => state.asOf);
let map = null;             // active tier renderer
let countiesPromise = null;
let fallbackTimer = null;
let lastStorySince = '1970-01-01'; // first poll backfills existing clusters, then advances to the newest updated_at
const stateCentroids = {};  // fips -> [lat, lon]

/* ---------------- tiny utils ---------------- */

const $ = (sel) => document.querySelector(sel);

function toast(msg, ms = 4000) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.classList.add('fade'); setTimeout(() => t.remove(), 600); }, ms);
}

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

/* ---------------- boot ---------------- */

async function boot() {
  // theme — Newsroom (light) by default; a previously saved theme wins
  try { state.theme = localStorage.getItem(THEME_KEY) || 'newsroom'; } catch (e) { /* noop */ }
  document.body.dataset.theme = state.theme;
  // race-type for the value map modes — Senate by default, persisted
  try { state.raceType = localStorage.getItem(RACE_TYPE_KEY) || 'senate'; } catch (e) { /* noop */ }
  if (!RACE_TYPES.includes(state.raceType)) state.raceType = 'senate';
  $('#theme-select').value = state.theme;
  $('#theme-select').addEventListener('change', (e) => setTheme(e.target.value));

  // sound
  const sound = (window.pgSound = new SoundEngine());
  sound.armGesture();
  const soundBtn = $('#sound-toggle');
  soundBtn.classList.toggle('off', !sound.enabled);
  soundBtn.addEventListener('click', () => soundBtn.classList.toggle('off', !sound.toggle()));

  // boundary data — states are required for every renderer
  state.statesGeo = await api.staticJson('/static/data/us_states.json');
  if (!state.statesGeo) {
    $('#map-root').innerHTML = '<div class="empty" style="margin:40px">Boundary data missing: /static/data/us_states.json did not load. The map cannot start without it.</div>';
    return;
  }
  const statesByFips = {};
  const statesList = [];
  for (const f of state.statesGeo.features) {
    statesByFips[f.id] = { key: f.id, name: f.properties.name };
    statesList.push({ key: f.id, name: f.properties.name });
    const info = featureInfo(f);
    stateCentroids[f.id] = [info.centroid[1], info.centroid[0]];
  }
  statesList.sort((a, b) => a.name.localeCompare(b.name));

  // mutable closure slots used by inner functions called during boot
  let lastLegend = null;   // last /api/map/values legend info
  let tip = null;          // hover tooltip element
  let priorDistricts = null; // {visible, mapTier} saved when House pins districts

  /* ----- the callback bag (fixed closure DI) ----- */
  const bag = {
    api, navigate, toast,
    getMap: () => map,
    statesByFips,
    states: statesList,
    territories: TERRITORIES,
    refreshWatchlist: () => feed && feed.refreshWatchlist(),
    flyToState: (fips) => map && map.flyToFeature('state', fips),
    flyToCounty: async (geoid) => {
      await ensureCounties();
      if (map) {
        map.flyToFeature('county', geoid);
        // county layer may not have this feature yet on tier1 until mesh built
        map.flyToFeature('state', geoid.slice(0, 2));
        map.flyToFeature('county', geoid);
      }
    },
    countyName: (geoid) => {
      const f = state.countiesGeo?.features.find((x) => x.id === geoid);
      return f ? `${f.properties.NAME} ${f.properties.LSAD || ''}`.trim() : null;
    },
    openAnalyst: (type, id, label) => { analyst.setEntity(type, id, label); analyst.openPanel(); },
    setAnalystContext: (type, id, label) => analyst.setEntity(type, id, label),
    activateElectionNight: (raceId) => { electionNight.activate(raceId); ensureCounties(); },
    onCountyColors: (colors) => { if (map) map.setOverrideColors('county', colors); },
    playFanfare: () => sound.callFanfare(),
    setBuilderMode: (on) => { state.builderActive = on; },
    onAsOf: (asOf) => setAsOf(asOf),
  };

  /* ----- components (instantiated once) ----- */
  const pane = new SlidePane($('#pane'), bag);
  const feed = new Feed($('#feed'), bag);
  const pollsWindow = new PollsWindow($('#view-root'), bag);
  const scorecard = new Scorecard($('#view-root'), bag);
  const lobbies = new Lobbies($('#view-root'), bag);
  const diagnostics = new Diagnostics($('#view-root'), bag);
  const settings = new Settings($('#view-root'), bag);
  const analyst = new Analyst(bag); // builds its own floating orb + docked panel
  const builder = new MapBuilder($('#pane'), bag);
  const electionNight = new ElectionNight($('#en-root'), bag);
  const scrubber = new TimeScrubber($('#scrubber'), bag);
  new CommandPalette($('#palette-root'), bag);
  $('#palette-hint').addEventListener('click', () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true })));

  window.pg = { state, pane, feed, pollsWindow, scorecard, diagnostics, settings, analyst, builder, electionNight, scrubber, bag };

  /* ----- map ----- */
  buildMap();
  buildHud();
  loadMapValues();
  probeDistricts();

  /* ----- backend status, pins ----- */
  api.status().then((s) => {
    state.status = s;
    if (!s) {
      toast('Backend unreachable — running on vendored data with empty-state panels', 6000);
      feed.setTransport('offline', false);
    }
    // version in the header — live /api/status.version wins over the hardcoded fallback
    if (s && s.version) $('#version-badge').textContent = 'v' + String(s.version).replace(/^v/i, '');
    if (s && s.election_night_mode) {
      const chip = $('#en-chip');
      chip.hidden = false;
      chip.style.cursor = 'pointer';
      chip.title = 'Election night mode is live — click to open';
      chip.addEventListener('click', () => bag.activateElectionNight(null));
      startChyron();
    }
  });
  api.mapPins().then((p) => p && map && map.setPins(p));

  /* ----- websocket + REST fallback ----- */
  const socket = new FeedSocket({
    onMessage: (frame) => handleFrame(frame),
    onStatus: (up) => {
      state.socketUp = up;
      feed.setTransport(up ? 'live · /ws/feed' : 'reconnecting…', up);
    },
    onFallbackStart: () => {
      feed.setTransport('REST fallback · 15s poll', false);
      clearInterval(fallbackTimer);
      fallbackTimer = setInterval(pollStories, 15000);
      pollStories();
    },
    onFallbackStop: () => clearInterval(fallbackTimer),
  });
  socket.connect();
  pollStories(); // backfill existing story clusters at boot; pushes dedup via feed.seen

  async function pollStories() {
    const rows = await api.stories(lastStorySince);
    if (!rows || !rows.length) return;
    for (const s of rows) {
      feed.addStory(s);
      if (s.updated_at && s.updated_at > lastStorySince) lastStorySince = s.updated_at;
    }
  }

  function handleFrame(frame) {
    const p = frame.payload || {};
    if (frame.type === 'story') {
      feed.addStory(p);
      sound.storyChime();
      threadForStory(p);
    } else if (frame.type === 'poll') {
      sound.pollChime();
      toast(`New poll: ${p.pollster || '?'} — ${p.race_name || 'race #' + p.race_id}`);
    } else if (frame.type === 'volatility') {
      // volatility feature removed from the UI — frame intentionally ignored
    } else if (frame.type === 'race_call') {
      sound.callFanfare();
      toast(`RACE CALLED by ${p.called_by}: ${p.winner_party} — race #${p.race_id}`);
    } else if (frame.type === 'results') {
      if (electionNight.active) electionNight.refresh();
    }
  }

  /** Election-night chyron — a slim marquee strip under the top bar cycling
      called/live races from /api/electionnight/live. Only ever started when
      /api/status reports election_night_mode; hidden again if the feed dries
      up. Duplicated track content makes the CSS loop seamless. */
  let chyronTimer = null;
  function startChyron() {
    const chy = $('#chyron');
    if (!chy || chyronTimer) return;
    const refresh = async () => {
      const data = await api.electionNightLive(null);
      const races = (data && data.races) || [];
      if (!races.length) { chy.hidden = true; return; }
      const items = races.slice(0, 30).map((r) => {
        const name = escapeHtml(r.name || `race #${r.race_id}`);
        if (r.called) {
          return `<span class="chy-item called" data-race="${r.race_id}">✓ ${name} — ${escapeHtml(r.called.winner_party || '?')} · called by ${escapeHtml(r.called.called_by || '?')}</span>`;
        }
        const tv = r.total_votes || {};
        const ranked = Object.entries(tv).sort((a, b) => b[1] - a[1]);
        const sum = ranked.reduce((a, [, v]) => a + (v || 0), 0);
        const lead = ranked.length && sum
          ? ` <span class="chy-lead">${escapeHtml(ranked[0][0])} ${((ranked[0][1] / sum) * 100).toFixed(0)}%</span>` : '';
        const pcts = (r.counties || []).map((c) => c.pct_reporting).filter((p) => p != null);
        const rep = pcts.length ? ` · ${(pcts.reduce((a, b) => a + b, 0) / pcts.length).toFixed(0)}% in` : '';
        return `<span class="chy-item" data-race="${r.race_id}">${name} — live${lead}${rep}</span>`;
      });
      const run = items.join('<span class="chy-sep">•</span>') + '<span class="chy-sep">•</span>';
      chy.innerHTML = `<div class="chy-track" style="--chy-dur:${Math.max(24, items.length * 7)}s">` +
        `<span class="chy-label">ELECTION NIGHT</span>${run}<span class="chy-label">ELECTION NIGHT</span>${run}</div>`;
      chy.hidden = false;
    };
    chy.addEventListener('click', (e) => {
      const it = e.target.closest('[data-race]');
      if (it && it.dataset.race && it.dataset.race !== 'null' && it.dataset.race !== 'undefined') {
        navigate(`#/race/${it.dataset.race}`);
      }
    });
    refresh();
    chyronTimer = setInterval(refresh, 60000);
  }

  /** Correlation threads: an arc is drawn ONLY when the backend flags a real
      link between two geographies (`linked_state_fips` on the story frame).
      There is no previous-story fallback — unrelated consecutive stories must
      never be joined by a stray cross-country line. No link → nothing drawn. */
  function threadForStory(p) {
    if (!map || !map.addThread) return;
    if (!p.linked_state_fips || !p.state_fips) return;
    const here = stateCentroids[String(p.state_fips).padStart(2, '0')];
    const linked = stateCentroids[String(p.linked_state_fips).padStart(2, '0')];
    if (here && linked && (here[0] !== linked[0] || here[1] !== linked[1])) map.addThread(linked, here);
  }

  /* ----- router ----- */
  window.addEventListener('hashchange', route);
  route();

  /* ---------------- inner functions using closure state ---------------- */

  function setTheme(name) {
    state.theme = name;
    document.body.dataset.theme = name;
    try { localStorage.setItem(THEME_KEY, name); } catch (e) { /* noop */ }
    if (map && map.refreshTheme) map.refreshTheme();
    if (state.builderActive) builder._paint();
    renderLegend();
  }

  function setAsOf(asOf) {
    state.asOf = asOf;
    const banner = $('#asof-banner');
    if (asOf) {
      banner.hidden = false;
      banner.textContent = `viewing as of ${asOf} — time capsule (all reads snapshot to this date)`;
    } else banner.hidden = true;
    loadMapValues();
    if (pane.current) pane.open(pane.current, true);
  }

  /* ----- map construction with graceful step-down ----- */

  function buildMap() {
    const chosen = chooseTier();
    state.tierSource = chosen.source;
    const hooks = {
      onPick: (unit) => {
        if (!unit) return;
        if (state.builderActive) { builder.handlePick(unit); return; }
        navigate(unit.tier === 'county' ? `#/county/${unit.key}` : `#/state/${unit.key}`);
      },
      onHover: (unit, x, y) => showHover(unit, x, y),
      onNeedCounties: () => ensureCounties(),
      onLodChange: (tierName) => {
        // House mode pins the district tier — don't let the zoom LOD steal it
        if (state.districtActive) return;
        state.mapTier = tierName;
        loadMapValues();
        updateTierBadge();
      },
    };
    const order = [];
    for (let t = chosen.tier; t <= 3; t++) order.push(t);
    for (const t of order) {
      try {
        if (t === 1) map = new Tier1Globe($('#map-root'), state.statesGeo, hooks);
        else if (t === 2) map = new Tier2Map($('#map-root'), state.statesGeo, hooks);
        else map = new Tier3List($('#map-root'), state.statesGeo, hooks);
        state.tier = t;
        break;
      } catch (e) {
        // graceful step-down: a tier's constructor threw — try the next one
        console.warn(`Tier ${t} failed (${e.message}); stepping down`);
        map = null;
      }
    }
    if (!map) {
      $('#map-root').innerHTML = '<div class="empty" style="margin:40px">No renderer could start.</div>';
      return;
    }
    if (state.countiesGeo && map.setCountiesGeo) map.setCountiesGeo(state.countiesGeo);
  }

  function rebuildMap() {
    if (map) { try { map.destroy(); } catch (e) { /* noop */ } }
    $('#map-root').innerHTML = '';
    map = null;
    buildMap();
    loadMapValues();
    updateTierBadge();
    if (state.builderActive) builder._paint();
  }

  async function ensureCounties() {
    if (state.countiesGeo) { map && map.setCountiesGeo && map.setCountiesGeo(state.countiesGeo); return state.countiesGeo; }
    countiesPromise ||= api.staticJson('/static/data/us_counties.json').then((geo) => {
      state.countiesGeo = geo;
      if (geo && map && map.setCountiesGeo) map.setCountiesGeo(geo);
      return geo;
    });
    return countiesPromise;
  }

  async function probeDistricts() {
    const geo = await api.staticJson('/static/data/us_districts.json');
    state.districtsAvailable = !!geo;
    const t = $('#districts-toggle');
    if (geo) {
      if (map && map.setDistrictsGeo) map.setDistrictsGeo(geo);
      if (t) { t.disabled = false; t.title = 'toggle congressional-district overlay'; }
    } else if (t) {
      t.disabled = true;
      t.title = 'district overlay unavailable — run scripts/build_boundaries.py';
    }
    // a House race-type chosen before the probe finished still gets its districts
    if (state.raceType === 'house') applyRaceTypeDistricts();
  }

  function districtsToggleOn() {
    const t = $('#districts-toggle');
    return !!(t && t.classList.contains('primary'));
  }

  function setDistrictsOverlay(on) {
    const t = $('#districts-toggle');
    if (!t || t.disabled) return;
    t.classList.toggle('primary', on);
    if (map && map.setDistrictsVisible) map.setDistrictsVisible(on);
  }

  /** House is a per-district contest: entering 'house' auto-enables the district
      overlay and pins the choropleth tier to 'district' (renderers fill it when
      the mode supports the tier; harmless outline-only otherwise). Leaving
      'house' restores whatever the user had. No-ops when district data is
      missing, so the map never blanks. Idempotent. */
  function applyRaceTypeDistricts() {
    const t = $('#districts-toggle');
    const house = state.raceType === 'house';
    const available = state.districtsAvailable && !!t && !t.disabled;
    if (house && available) {
      if (!priorDistricts) priorDistricts = { visible: districtsToggleOn(), mapTier: state.mapTier };
      state.districtActive = true;
      setDistrictsOverlay(true);
      state.mapTier = 'district';
      updateTierBadge();
    } else if (!house && priorDistricts) {
      setDistrictsOverlay(priorDistricts.visible);
      state.mapTier = priorDistricts.mapTier;
      priorDistricts = null;
      state.districtActive = false;
      updateTierBadge();
    }
  }

  /* ----- HUD ----- */

  function buildHud() {
    const hud = $('#map-hud');
    hud.innerHTML = `
      <div class="hud-row">
        <div class="seg-control" id="race-type-seg" role="group" title="race type driving the value map modes (forecast, average, lean, turnout)">
          ${RACE_TYPES.map((t) => `<button data-rt="${t}" class="${t === state.raceType ? 'on' : ''}">${t === 'president' ? 'President' : t === 'senate' ? 'Senate' : t === 'governor' ? 'Governor' : 'House'}</button>`).join('')}
        </div>
        <select id="mode-select" title="thematic map mode">
          ${ALL_MODES.map((m) => `<option value="${m.key}">${escapeHtml(m.label)}</option>`).join('')}
        </select>
        <button id="districts-toggle" disabled title="checking district data…">districts</button>
        <button id="snapshot-btn" title="download a PNG of the current map + legend">snapshot</button>
      </div>
      <div class="hud-card legend" id="legend"></div>
      <div class="hud-row territory-row" title="DC & the territories have no clickable map geometry (except PR) — this row is their doorway">
        <span class="territory-label">DC &amp; Territories</span>
        ${TERRITORIES.map((t) => `<button class="territory-chip" data-fips="${t.key}" title="${escapeHtml(t.name)}">${t.usps}</button>`).join('')}
      </div>
      <div class="hud-row">
        <span class="tier-badge" id="tier-badge"></span>
        <select id="tier-select" title="renderer tier override (saved)">
          <option value="auto">auto</option>
          <option value="2">2D map (default)</option>
          <option value="1">3D globe</option>
          <option value="3">list</option>
        </select>
      </div>`;
    $('#mode-select').value = state.mode;
    $('#mode-select').addEventListener('change', (e) => { state.mode = e.target.value; loadMapValues(); });
    for (const b of hud.querySelectorAll('#race-type-seg button')) {
      b.addEventListener('click', () => {
        if (state.raceType === b.dataset.rt) return;
        state.raceType = b.dataset.rt;
        try { localStorage.setItem(RACE_TYPE_KEY, state.raceType); } catch (e) { /* noop */ }
        for (const x of hud.querySelectorAll('#race-type-seg button')) x.classList.toggle('on', x === b);
        applyRaceTypeDistricts();
        loadMapValues();
      });
    }
    for (const c of hud.querySelectorAll('.territory-chip')) {
      c.addEventListener('click', () => navigate(`#/state/${c.dataset.fips}`));
    }
    $('#districts-toggle').addEventListener('click', () => {
      const on = $('#districts-toggle').classList.toggle('primary');
      if (map && map.setDistrictsVisible) map.setDistrictsVisible(on);
    });
    $('#snapshot-btn').addEventListener('click', () => {
      const canvas = map && map.getCanvas ? map.getCanvas() : null;
      const m = getMode(state.mode);
      const pal = themePalette();
      const legend = [0, 0.25, 0.5, 0.75, 1].map((t) => ({
        color: rgbCss(rampColor(t, m.ramp, pal)),
        label: t === 0 ? 'low' : t === 1 ? 'high' : '',
      }));
      downloadCanvas(
        composeSnapshot(canvas, {
          title: m.label,
          subtitle: `${state.mapTier} tier · ${state.asOf ? 'as of ' + state.asOf : 'live'} · pollgrid`,
          legend,
        }),
        'pollgrid-map.png');
    });
    $('#tier-select').value = getOverride();
    $('#tier-select').addEventListener('change', (e) => { setOverride(e.target.value); rebuildMap(); });
    updateTierBadge();
    renderLegend();
  }

  function updateTierBadge() {
    const b = $('#tier-badge');
    if (b) b.textContent = `tier ${state.tier} (${state.tierSource}) · ${state.mapTier} LOD`;
  }

  function renderLegend() {
    const box = $('#legend');
    if (!box) return;
    const m = getMode(state.mode);
    const pal = themePalette();
    const stops = [];
    for (let i = 0; i <= 10; i++) stops.push(rgbCss(rampColor(i / 10, m.ramp, pal)) + ` ${i * 10}%`);
    const l = lastLegend;
    box.innerHTML = `
      <div style="font-size:11px">${escapeHtml(m.label)}</div>
      <div class="ramp" style="background:linear-gradient(90deg, ${stops.join(',')})"></div>
      <div class="legend-lab">
        <span>${l ? escapeHtml(m.fmt(m.ramp === 'diverging' ? Math.max(Math.abs(l.min), Math.abs(l.max)) : l.min)) : '—'}</span>
        <span>${l ? escapeHtml(l.label || '') : 'no data'}</span>
        <span>${l ? escapeHtml(m.fmt(m.ramp === 'diverging' ? -Math.max(Math.abs(l.min), Math.abs(l.max)) : l.max)) : '—'}</span>
      </div>
      <div class="legend-note">${l ? (l.hasDerived ? 'hatched / badged units are <b>derived</b> estimates, not direct measurements' : '') : 'backend offline — map shows boundaries only'}</div>`;
  }

  /* ----- choropleth ----- */

  async function loadMapValues() {
    if (!map) return;
    const m = getMode(state.mode);
    const tier = m.tiers.includes(state.mapTier) ? state.mapTier : 'state';
    const res = await api.mapValues(m.key, tier, m.raceTyped ? { race_type: state.raceType } : {});
    if (!map) return;
    if (!res || !res.values) {
      lastLegend = null;
      map.setChoropleth({ tier, values: null, confidence: null, rampType: m.ramp, min: 0, max: 1, fmt: m.fmt, label: m.label });
      renderLegend();
      return;
    }
    let min = res.legend?.min, max = res.legend?.max;
    const nums = Object.values(res.values);
    if (min == null) min = Math.min(...nums);
    if (max == null) max = Math.max(...nums);
    if (m.ramp === 'diverging') {
      const a = Math.max(Math.abs(min), Math.abs(max)) || 1;
      min = -a; max = a;
    }
    lastLegend = { min, max, label: res.legend?.label || '', hasDerived: Object.values(res.confidence || {}).includes('derived') };
    map.setChoropleth({
      tier, values: res.values, confidence: res.confidence || null,
      rampType: m.ramp, min, max, fmt: m.fmt, label: m.label,
    });
    renderLegend();
  }

  /* ----- hover tooltip ----- */

  function showHover(unit, x, y) {
    if (!unit) { if (tip) { tip.remove(); tip = null; } return; }
    if (!tip) { tip = document.createElement('div'); tip.className = 'hover-tip'; document.body.appendChild(tip); }
    const m = getMode(state.mode);
    const ch = map.choropleth || {};
    const v = ch.values && ch.tier === unit.tier ? ch.values[unit.key] : undefined;
    const conf = ch.confidence && ch.confidence[unit.key];
    tip.innerHTML = `<b>${escapeHtml(unit.name || unit.key)}</b><br>
      <span class="tip-val">${v !== undefined ? m.fmt(v) : 'no data'}</span>
      ${conf === 'derived' ? ' <span class="chip warn">derived</span>' : ''}`;
    tip.style.left = `${Math.min(window.innerWidth - 280, x + 14)}px`;
    tip.style.top = `${y + 14}px`;
  }

  /* ----- routing ----- */

  function showView(which) {
    const vr = $('#view-root');
    vr.hidden = false;
    $('#map-hud').hidden = true; // full-page views cover the map; its HUD must not bleed through
    if (which === 'polls') pollsWindow.show();
    else if (which === 'scorecard') scorecard.show();
    else if (which === 'lobbies') lobbies.show();
    else if (which === 'diagnostics') diagnostics.show();
    else if (which === 'settings') settings.show();
  }

  function route() {
    const hash = location.hash || '#/';
    const parts = hash.replace(/^#\//, '').split('/');
    const page = parts[0] || '';

    // nav highlighting
    for (const a of document.querySelectorAll('#topnav a')) {
      a.classList.toggle('active', a.getAttribute('href') === `#/${page}` || (a.getAttribute('href') === '#/' && !page));
    }

    // builder teardown when leaving
    if (page !== 'builder' && state.builderActive) {
      state.builderActive = false;
      builder.deactivate();
      pane.close();
    }
    const vr = $('#view-root');

    // '#/analyst' is now just an alias that opens the docked panel over whatever
    // view is currently showing — it never takes over the page.
    if (page === 'analyst') { analyst.openPanel(); return; }

    if (page === 'polls' || page === 'scorecard' || page === 'lobbies' || page === 'settings' || page === 'diagnostics') {
      bag.setAnalystContext('nation', 'US', 'the national picture');
      pane.close();
      showView(page);
      return;
    }
    vr.hidden = true;
    $('#map-hud').hidden = false;

    if (page === 'builder') {
      state.builderActive = true;
      pane.close();
      builder.activate(parts[1] || null);
      return;
    }

    // Each selection auto-updates the Analyst's context (SlidePane refines the
    // label once its data loads; the entity_type+id stay stable so the chat is
    // kept). The docked panel shows "context: {label}" and tracks these.
    if (page === 'race') {
      bag.setAnalystContext('race', parts[1], `race #${parts[1]}`);
      pane.open({ type: 'race', id: parts[1] });
      return;
    }
    if (page === 'state') {
      bag.setAnalystContext('state', parts[1], statesByFips[parts[1]]?.name || `state ${parts[1]}`);
      pane.open({ type: 'state', id: parts[1] });
      if (map) map.flyToFeature('state', parts[1]);
      return;
    }
    if (page === 'county') {
      bag.setAnalystContext('county_equivalent', parts[1], bag.countyName(parts[1]) || `county ${parts[1]}`);
      pane.open({ type: 'county', id: parts[1] });
      bag.flyToCounty(parts[1]);
      return;
    }
    if (page === 'district') {
      bag.setAnalystContext('congressional_district', parts[1], `district ${parts[1]}`);
      pane.open({ type: 'district', id: parts[1] });
      return;
    }
    if (page === 'candidate') {
      bag.setAnalystContext('candidate', parts[1], `candidate #${parts[1]}`);
      pane.open({ type: 'candidate', id: parts[1] });
      return;
    }
    if (page === 'party') {
      bag.setAnalystContext('party', parts[1], `party #${parts[1]}`);
      pane.open({ type: 'party', id: parts[1] });
      return;
    }
    if (page === 'story') { pane.open({ type: 'story', id: parts[1] }); return; }
    if (page === 'lobby') { pane.open({ type: 'lobby', id: parts[1] }); return; }

    // default: plain map — nothing selected, so the Analyst reads the nation
    bag.setAnalystContext('nation', 'US', 'the national picture');
    pane.close();
  }
}

boot();
