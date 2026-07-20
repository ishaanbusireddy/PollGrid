/* Tier2Map.js — the 2D-canvas fallback renderer. Classic composite
   Albers-USA projection (lower 48 on standard parallels 29.5°/45.5°, Alaska
   and Hawaii — plus a Puerto Rico inset — projected separately, then
   translated + scaled). Same choropleth modes and confidence hatching as
   Tier 1; picking is point-in-polygon in projected space via Path2D
   isPointInPath under the live pan/zoom transform. No globe frills. */

import { makeAlbersUsa, eachPolygon, themePalette, rampColor } from './geometry.js';

const REF_W = 960, REF_H = 600;
const COUNTY_LOD_SCALE = 2.6;
const K_MIN = 0.6, K_MAX = 12;

/* game-style keyboard nav: WASD/arrows pan, Q/E and +/- zoom */
const NAV_KEYS = new Set([
  'w', 'a', 's', 'd', 'q', 'e',
  'arrowup', 'arrowdown', 'arrowleft', 'arrowright',
  '+', '=', '-', '_',
]);

function isTypingTarget(t) {
  if (!t) return false;
  const tag = t.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable;
}

export class Tier2Map {
  constructor(container, statesGeo, hooks = {}) {
    this.container = container;
    this.hooks = hooks;
    this.canvas = document.createElement('canvas');
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    if (!this.ctx) { this.canvas.remove(); throw new Error('2D canvas unavailable'); }

    this.project = makeAlbersUsa();
    this.states = this._prepareLayer(statesGeo.features);
    this.counties = null;
    this.countiesRequested = false;
    this.districts = null;
    this.districtsVisible = false;
    this.showCounties = false;

    this.view = { x: REF_W / 2, y: REF_H / 2, k: 1 }; // pan/zoom in reference coords
    this.flight = null;
    this.targetK = null;                     // eased wheel-zoom target (null = settled)
    this._zoomAnchor = null;                  // {sx,sy} device-px cursor the zoom pivots on
    this._keys = new Set();                  // currently-held nav keys
    this._navRaf = null;                     // rAF handle, live ONLY while a key is held
    this.threads = [];                       // 2D animated arcs
    this.choropleth = { tier: 'state', values: null, confidence: null, rampType: 'sequential', min: 0, max: 1 };
    this.override = null;
    this.pinsData = null;

    this.refreshTheme();
    this._bindEvents();
    this.resize();
    this._raf = requestAnimationFrame(() => this._frame());
  }

  _prepareLayer(features) {
    const out = { features, paths: [], bbox: [], centroid: [] };
    for (const f of features) {
      const p = new Path2D();
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      let cx = 0, cy = 0, cn = 0;
      eachPolygon(f.geometry, (ring) => {
        ring.forEach(([lon, lat], i) => {
          const [x, y] = this.project(lon, lat);
          if (i === 0) p.moveTo(x, y); else p.lineTo(x, y);
          if (x < minX) minX = x; if (x > maxX) maxX = x;
          if (y < minY) minY = y; if (y > maxY) maxY = y;
          cx += x; cy += y; cn++;
        });
        p.closePath();
      });
      out.paths.push(p);
      out.bbox.push([minX, minY, maxX, maxY]);
      out.centroid.push([cx / (cn || 1), cy / (cn || 1)]);
    }
    return out;
  }

  setCountiesGeo(geo) { if (geo && !this.counties) this.counties = this._prepareLayer(geo.features); }
  setDistrictsGeo(geo) { if (geo) this.districts = this._prepareLayer(geo.features); }
  setDistrictsVisible(v) { this.districtsVisible = !!v; }
  setChoropleth(cfg) { this.choropleth = { ...this.choropleth, ...cfg }; }
  setOverrideColors(tier, colors) { this.override = colors ? { tier, colors } : null; }
  setPins(pins) { this.pinsData = pins; }
  refreshTheme() {
    this.palette = themePalette();
  }

  /* ----- camera ----- */

  flyTo(lat, lon, zoom, ms = 800) {
    this.targetK = null; // fly-to owns the camera; drop any pending wheel-zoom
    const [x, y] = this.project(lon, lat);
    this.flight = { from: { ...this.view }, to: { x, y, k: zoom || this.view.k }, t0: performance.now(), dur: ms };
  }

  flyToFeature(tier, key) {
    const layer = tier === 'county' ? this.counties : this.states;
    if (!layer) return;
    const i = layer.features.findIndex((f) => f.id === key);
    if (i < 0) return;
    const [minX, minY, maxX, maxY] = layer.bbox[i];
    const k = Math.min(18, Math.max(1, 0.75 * Math.min(REF_W / (maxX - minX + 1), REF_H / (maxY - minY + 1))));
    this.targetK = null; // fly-to owns the camera; drop any pending wheel-zoom
    this.flight = {
      from: { ...this.view },
      to: { x: (minX + maxX) / 2, y: (minY + maxY) / 2, k },
      t0: performance.now(), dur: 800,
    };
  }

  addThread(a, b) {
    const pa = this.project(a[1], a[0]), pb = this.project(b[1], b[0]);
    this.threads.push({ a: pa, b: pb, birth: performance.now() });
    if (this.threads.length > 12) this.threads.shift();
  }

  /* ----- transform & picking ----- */

  _applyTransform(ctx) {
    const w = this.canvas.width, h = this.canvas.height;
    const base = Math.min(w / REF_W, h / REF_H);
    const k = base * this.view.k;
    const cx = this.view.x || REF_W / 2, cy = this.view.y || REF_H / 2;
    ctx.setTransform(k, 0, 0, k, w / 2 - cx * k, h / 2 - cy * k);
  }

  pick(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = this.canvas.width / rect.width;
    const x = (clientX - rect.left) * dpr, y = (clientY - rect.top) * dpr;
    const ctx = this.ctx;
    this._applyTransform(ctx);
    const layer = this.showCounties && this.counties ? this.counties : this.states;
    for (let i = 0; i < layer.features.length; i++) {
      if (ctx.isPointInPath(layer.paths[i], x, y)) {
        const f = layer.features[i];
        if (layer === this.counties) {
          return { tier: 'county', key: f.id, name: `${f.properties.NAME} ${f.properties.LSAD || ''}`.trim(), stateFips: f.properties.STATE };
        }
        return { tier: 'state', key: f.id, name: f.properties.name };
      }
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    return null;
  }

  _bindEvents() {
    const c = this.canvas;
    this._drag = null;
    this._onDown = (e) => { this.targetK = null; this._drag = { x: e.clientX, y: e.clientY, vx: this.view.x || REF_W / 2, vy: this.view.y || REF_H / 2, moved: false }; c.setPointerCapture(e.pointerId); };
    this._onMove = (e) => {
      if (this._drag && (e.buttons & 1)) {
        const rect = c.getBoundingClientRect();
        const base = Math.min(this.canvas.width / REF_W, this.canvas.height / REF_H) * this.view.k / (this.canvas.width / rect.width);
        const dx = (e.clientX - this._drag.x) / base, dy = (e.clientY - this._drag.y) / base;
        if (Math.abs(dx) + Math.abs(dy) > 2) this._drag.moved = true;
        this.view.x = this._drag.vx - dx;
        this.view.y = this._drag.vy - dy;
        this.flight = null;
      } else {
        this._hoverXY = [e.clientX, e.clientY];
      }
    };
    this._onUp = (e) => {
      const moved = this._drag && this._drag.moved;
      this._drag = null;
      if (!moved) this.hooks.onPick && this.hooks.onPick(this.pick(e.clientX, e.clientY), e);
    };
    // smooth wheel zoom, anchored to the cursor: set a TARGET zoom and let _frame
    // ease view.k toward it (the geographic point under the pointer stays put the
    // whole animation, not just at the end). deltaMode is normalized so a mouse
    // wheel (lines/pages) and a trackpad (pixels) feel the same.
    this._onWheel = (e) => {
      e.preventDefault();
      const rect = c.getBoundingClientRect();
      const dpr = this.canvas.width / (rect.width || 1);
      this._zoomAnchor = { sx: (e.clientX - rect.left) * dpr, sy: (e.clientY - rect.top) * dpr };
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? (this.canvas.height || REF_H) : 1;
      const dy = e.deltaY * unit;
      const from = this.targetK != null ? this.targetK : this.view.k;
      this.targetK = Math.max(K_MIN, Math.min(K_MAX, from * Math.exp(-dy * 0.0018)));
      this.flight = null;
    };
    this._onLeave = () => { this._hoverXY = null; this.hooks.onHover && this.hooks.onHover(null); };
    c.addEventListener('pointerdown', this._onDown);
    c.addEventListener('pointermove', this._onMove);
    c.addEventListener('pointerup', this._onUp);
    c.addEventListener('wheel', this._onWheel, { passive: false });
    c.addEventListener('pointerleave', this._onLeave);
    // keyboard nav is document-wide (the canvas isn't focusable); ignored while typing
    this._onKeyDown = (e) => {
      if (isTypingTarget(e.target)) return;
      const k = e.key.toLowerCase();
      if (!NAV_KEYS.has(k)) return;
      e.preventDefault();
      if (!this._keys.has(k)) { this._keys.add(k); this._startNav(); }
    };
    this._onKeyUp = (e) => {
      const k = e.key.toLowerCase();
      this._keys.delete(k);
    };
    this._onBlur = () => { this._keys.clear(); };
    window.addEventListener('keydown', this._onKeyDown);
    window.addEventListener('keyup', this._onKeyUp);
    window.addEventListener('blur', this._onBlur);
    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
  }

  /** Light, generous pan clamp — the map moves freely with plenty of overscroll,
      but the view centre can't wander so far that the country leaves the screen.
      Also enforces the zoom range. */
  _clampView() {
    this.view.k = Math.max(K_MIN, Math.min(K_MAX, this.view.k));
    const mx = REF_W * 0.75, my = REF_H * 0.75;
    this.view.x = Math.max(-mx, Math.min(REF_W + mx, this.view.x));
    this.view.y = Math.max(-my, Math.min(REF_H + my, this.view.y));
  }

  /* Key-gated rAF: starts on the first keydown, self-stops when every key is
     released — never a permanent loop of its own. */
  _startNav() {
    if (this._navRaf != null) return;
    this._navLast = performance.now();
    this._navRaf = requestAnimationFrame((t) => this._navFrame(t));
  }

  _navFrame(now) {
    if (!this._keys.size) { this._navRaf = null; return; }
    const dt = Math.min(0.05, (now - (this._navLast || now)) / 1000); // seconds, clamped
    this._navLast = now;
    const w = this.canvas.width, h = this.canvas.height;
    const scale = Math.min(w / REF_W, h / REF_H) * this.view.k; // device-px per ref-unit
    const has = (k) => this._keys.has(k);
    // pan: ~90%/sec of the visible span, so it feels the same at every zoom
    let dx = 0, dy = 0;
    if (has('a') || has('arrowleft')) dx -= 1;
    if (has('d') || has('arrowright')) dx += 1;
    if (has('w') || has('arrowup')) dy -= 1;
    if (has('s') || has('arrowdown')) dy += 1;
    if (dx || dy) {
      this.view.x += dx * 0.9 * (w / scale) * dt;
      this.view.y += dy * 0.9 * (h / scale) * dt;
      this.flight = null;
    }
    // zoom toward the view centre
    let zf = 0;
    if (has('e') || has('+') || has('=')) zf += 1;
    if (has('q') || has('-') || has('_')) zf -= 1;
    if (zf) { this.view.k *= Math.exp(zf * 1.6 * dt); this.flight = null; this.targetK = null; }
    this._clampView();
    this._navRaf = requestAnimationFrame((t) => this._navFrame(t));
  }

  resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = this.container.clientWidth || 800, h = this.container.clientHeight || 600;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
  }

  _colorFor(layerTier, key) {
    const pal = this.palette;
    if (this.override && this.override.tier === layerTier && this.override.colors.has(key)) {
      const c = this.override.colors.get(key);
      return { css: `rgb(${c[0]},${c[1]},${c[2]})`, conf: 'measured' };
    }
    const ch = this.choropleth;
    if (ch.values && ch.tier === layerTier && ch.values[key] !== undefined) {
      const span = (ch.max - ch.min) || 1;
      const t = (ch.values[key] - ch.min) / span;
      const c = rampColor(ch.rampType === 'diverging' ? 1 - t : t, ch.rampType, pal);
      return { css: `rgb(${c[0]},${c[1]},${c[2]})`, conf: ch.confidence && ch.confidence[key] === 'derived' ? 'derived' : 'measured' };
    }
    const c = pal.low;
    return { css: `rgba(${c[0]},${c[1]},${c[2]},0.4)`, conf: 'none' };
  }

  _frame() {
    this._raf = requestAnimationFrame(() => this._frame());
    const now = performance.now();
    if (this.flight) {
      const f = this.flight;
      const t = Math.min(1, (now - f.t0) / f.dur);
      const e = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
      this.view.x = (f.from.x || REF_W / 2) + (f.to.x - (f.from.x || REF_W / 2)) * e;
      this.view.y = (f.from.y || REF_H / 2) + (f.to.y - (f.from.y || REF_H / 2)) * e;
      this.view.k = f.from.k + (f.to.k - f.from.k) * e;
      if (t >= 1) this.flight = null;
    }
    // eased wheel zoom toward targetK, keeping the cursor anchor fixed each step
    if (this.targetK != null) {
      const k0 = this.view.k;
      let k1 = k0 + (this.targetK - k0) * 0.22;
      if (Math.abs(this.targetK - k1) < 0.001) k1 = this.targetK;
      if (k1 !== k0) {
        const w = this.canvas.width, h = this.canvas.height;
        const base = Math.min(w / REF_W, h / REF_H);
        const kOldPx = base * k0, kNewPx = base * k1;
        const a = this._zoomAnchor || { sx: w / 2, sy: h / 2 };
        this.view.x += (a.sx - w / 2) * (1 / kOldPx - 1 / kNewPx);
        this.view.y += (a.sy - h / 2) * (1 / kOldPx - 1 / kNewPx);
        this.view.k = k1;
        this._clampView();
      }
      if (this.view.k === this.targetK) { this.targetK = null; this._zoomAnchor = null; }
    }
    const wantCounties = this.view.k >= COUNTY_LOD_SCALE;
    if (wantCounties && !this.countiesRequested) {
      this.countiesRequested = true;
      this.hooks.onNeedCounties && this.hooks.onNeedCounties();
    }
    if (wantCounties !== this.showCounties) {
      this.showCounties = wantCounties;
      this.hooks.onLodChange && this.hooks.onLodChange(wantCounties ? 'county' : 'state');
    }
    if (this._hoverXY && this.hooks.onHover) {
      const [hx, hy] = this._hoverXY;
      this._hoverXY = null;
      this.hooks.onHover(this.pick(hx, hy), hx, hy);
    }
    this._draw(now);
  }

  _draw(now) {
    const ctx = this.ctx, pal = this.palette;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = `rgb(${pal.bg[0]},${pal.bg[1]},${pal.bg[2]})`;
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    this._applyTransform(ctx);

    const lineCss = `rgba(${pal.line[0]},${pal.line[1]},${pal.line[2]},0.9)`;
    const accentCss = `rgb(${pal.accent[0]},${pal.accent[1]},${pal.accent[2]})`;
    const k = this.view.k;

    const drawLayer = (layer, tierName, strokeCss, lw) => {
      for (let i = 0; i < layer.features.length; i++) {
        const { css } = this._colorFor(tierName, layer.features[i].id);
        ctx.fillStyle = css;
        ctx.fill(layer.paths[i]);
        ctx.strokeStyle = strokeCss;
        ctx.lineWidth = lw / k;
        ctx.stroke(layer.paths[i]);
      }
    };

    if (this.choropleth.tier === 'district' && this.districts) {
      // House mode: fill congressional districts, keep state outlines for context
      drawLayer(this.districts, 'district', lineCss, 0.4);
      ctx.strokeStyle = accentCss;
      ctx.lineWidth = 1.2 / k;
      for (const p of this.states.paths) ctx.stroke(p);
    } else if (this.showCounties && this.counties && this.choropleth.tier === 'county') {
      // only color the county layer when the choropleth is actually county-keyed;
      // a state-keyed choropleth at county zoom keeps coloring the state layer below
      drawLayer(this.counties, 'county', lineCss, 0.5);
      // state outlines on top
      ctx.strokeStyle = accentCss;
      ctx.lineWidth = 1.4 / k;
      for (const p of this.states.paths) ctx.stroke(p);
    } else {
      drawLayer(this.states, 'state', accentCss, 1);
    }
    if (this.districtsVisible && this.districts) {
      ctx.strokeStyle = `rgba(${pal.text[0]},${pal.text[1]},${pal.text[2]},0.55)`;
      ctx.lineWidth = 0.7 / k;
      for (const p of this.districts.paths) ctx.stroke(p);
    }

    // pins
    if (this.pinsData) {
      ctx.fillStyle = accentCss;
      for (const p of this.pinsData) {
        const [x, y] = this.project(p.lon, p.lat);
        ctx.beginPath(); ctx.arc(x, y, 3.5 / k, 0, Math.PI * 2); ctx.fill();
      }
    }

    // correlation threads (2D arcs with a moving head)
    const dead = [];
    for (const th of this.threads) {
      const age = (now - th.birth) / 1000;
      if (age > 5.5) { dead.push(th); continue; }
      const progress = Math.min(1, age / 1.8);
      const fade = age < 4 ? 1 : Math.max(0, 1 - (age - 4) / 1.5);
      const mx = (th.a[0] + th.b[0]) / 2, my = (th.a[1] + th.b[1]) / 2 - 60;
      ctx.strokeStyle = accentCss;
      ctx.globalAlpha = 0.3 * fade;
      ctx.lineWidth = 1.4 / k;
      ctx.beginPath();
      ctx.moveTo(th.a[0], th.a[1]);
      // draw quadratic arc up to progress
      const steps = 40;
      for (let s = 1; s <= steps * progress; s++) {
        const t = s / steps;
        const x = (1 - t) ** 2 * th.a[0] + 2 * (1 - t) * t * mx + t * t * th.b[0];
        const y = (1 - t) ** 2 * th.a[1] + 2 * (1 - t) * t * my + t * t * th.b[1];
        ctx.lineTo(x, y);
      }
      ctx.stroke();
      // head
      const t = progress;
      const hx = (1 - t) ** 2 * th.a[0] + 2 * (1 - t) * t * mx + t * t * th.b[0];
      const hy = (1 - t) ** 2 * th.a[1] + 2 * (1 - t) * t * my + t * t * th.b[1];
      ctx.globalAlpha = fade;
      ctx.fillStyle = accentCss;
      ctx.beginPath(); ctx.arc(hx, hy, 3 / k, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
    }
    for (const d of dead) this.threads.splice(this.threads.indexOf(d), 1);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  getCanvas() { return this.canvas; }

  destroy() {
    cancelAnimationFrame(this._raf);
    if (this._navRaf != null) { cancelAnimationFrame(this._navRaf); this._navRaf = null; }
    window.removeEventListener('resize', this._onResize);
    window.removeEventListener('keydown', this._onKeyDown);
    window.removeEventListener('keyup', this._onKeyUp);
    window.removeEventListener('blur', this._onBlur);
    const c = this.canvas;
    c.removeEventListener('pointerdown', this._onDown);
    c.removeEventListener('pointermove', this._onMove);
    c.removeEventListener('pointerup', this._onUp);
    c.removeEventListener('wheel', this._onWheel);
    c.removeEventListener('pointerleave', this._onLeave);
    c.remove();
  }
}
