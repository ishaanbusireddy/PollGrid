/* Tier2Map.js — the 2D-canvas fallback renderer. Classic composite
   Albers-USA projection (lower 48 on standard parallels 29.5°/45.5°, Alaska
   and Hawaii — plus a Puerto Rico inset — projected separately, then
   translated + scaled). Same choropleth modes and confidence hatching as
   Tier 1; picking is point-in-polygon in projected space via Path2D
   isPointInPath under the live pan/zoom transform. No globe frills. */

import { makeAlbersUsa, eachPolygon, themePalette, rampColor } from './geometry.js';

const REF_W = 960, REF_H = 600;
const COUNTY_LOD_SCALE = 2.6;

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

    this.view = { x: 0, y: 0, k: 1 };       // pan/zoom in reference coords
    this.flight = null;
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
    // hatch pattern for 'derived' confidence
    const pc = document.createElement('canvas');
    pc.width = pc.height = 6;
    const px = pc.getContext('2d');
    px.strokeStyle = 'rgba(0,0,0,0.35)';
    px.lineWidth = 1.4;
    px.beginPath(); px.moveTo(-1, 7); px.lineTo(7, -1); px.stroke();
    this.hatch = this.ctx.createPattern(pc, 'repeat');
  }

  /* ----- camera ----- */

  flyTo(lat, lon, zoom, ms = 800) {
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
    this._onDown = (e) => { this._drag = { x: e.clientX, y: e.clientY, vx: this.view.x || REF_W / 2, vy: this.view.y || REF_H / 2, moved: false }; c.setPointerCapture(e.pointerId); };
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
    this._onWheel = (e) => {
      e.preventDefault();
      this.view.k = Math.max(0.8, Math.min(30, this.view.k * Math.exp(-e.deltaY * 0.0012)));
      this.flight = null;
    };
    this._onLeave = () => { this._hoverXY = null; this.hooks.onHover && this.hooks.onHover(null); };
    c.addEventListener('pointerdown', this._onDown);
    c.addEventListener('pointermove', this._onMove);
    c.addEventListener('pointerup', this._onUp);
    c.addEventListener('wheel', this._onWheel, { passive: false });
    c.addEventListener('pointerleave', this._onLeave);
    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
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
        const { css, conf } = this._colorFor(tierName, layer.features[i].id);
        ctx.fillStyle = css;
        ctx.fill(layer.paths[i]);
        if (conf === 'derived' && this.hatch) { ctx.fillStyle = this.hatch; ctx.fill(layer.paths[i]); }
        ctx.strokeStyle = strokeCss;
        ctx.lineWidth = lw / k;
        ctx.stroke(layer.paths[i]);
      }
    };

    if (this.showCounties && this.counties) {
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
    window.removeEventListener('resize', this._onResize);
    const c = this.canvas;
    c.removeEventListener('pointerdown', this._onDown);
    c.removeEventListener('pointermove', this._onMove);
    c.removeEventListener('pointerup', this._onUp);
    c.removeEventListener('wheel', this._onWheel);
    c.removeEventListener('pointerleave', this._onLeave);
    c.remove();
  }
}
