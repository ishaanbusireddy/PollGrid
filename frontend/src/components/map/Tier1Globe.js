/* Tier1Globe.js — THE CENTERPIECE. A hand-rolled WebGL2 orthographic globe:
   no three.js, raw shaders only.

   - Sphere: lat/lon triangle mesh; day/night terminator computed in-shader
     from the real UTC subsolar point (object-space lighting so the terminator
     stays glued to geography as the camera rotates).
   - City lights: hardcoded US city coords, visible only on the night side.
   - States: polygon outer rings ear-clipped in 2D lon/lat space per polygon
     (multi-polygon states MI/AK/HI handled per-polygon; AK's antimeridian
     rings are shifted into a continuous lon domain before clipping).
   - Counties: built lazily and shown past a zoom LOD threshold.
   - Choropleth: per-feature colors in a small RGBA texture indexed by a
     per-vertex feature id; texel alpha encodes confidence — 'derived' rows
     render with screen-space hatching (the precinct-honesty rule), missing
     values render faint.
   - Fly-to camera easing, and particle "correlation threads": animated
     great-circle arcs with a bright head pulse.

   PICKING (documented choice): inverse-orthographic unprojection + 2D
   point-in-polygon on the raw lon/lat rings — screen point → ndc → view
   sphere point → inverse camera rotation → lat/lon → ray-cast against
   feature rings (bbox prefiltered). Chosen over a color-picking framebuffer
   because it needs no extra render pass and reuses the same ring data the
   triangulator already normalized. */

import { llToXyz, earClip, eachPolygon, normalizeRing, featureInfo, pointInFeature, mat4, greatCircle } from './geometry.js';
import { CITY_LIGHTS } from './cities.js';
import { themePalette, rampColor } from './geometry.js';

const COUNTY_LOD_ZOOM = 3.4;
const G_ZOOM_MIN = 1.3, G_ZOOM_MAX = 30;

/* game-style keyboard nav (shared with Tier2): WASD/arrows pan, Q/E and +/- zoom */
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

/* ---------------- shaders ---------------- */

const SPHERE_VS = `#version 300 es
precision highp float;
in vec3 aPos;
uniform mat4 uModel; uniform vec2 uScale;
out vec3 vObj; out float vRotZ;
void main(){
  vec4 r = uModel * vec4(aPos,1.0);
  vObj = aPos; vRotZ = r.z;
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5, 1.0);
}`;

const SPHERE_FS = `#version 300 es
precision highp float;
in vec3 vObj; in float vRotZ;
uniform vec3 uSun; uniform vec3 uDay; uniform vec3 uNight;
out vec4 frag;
void main(){
  vec3 n = normalize(vObj);
  float sunDot = dot(n, uSun);
  float daymix = smoothstep(-0.08, 0.12, sunDot);
  vec3 col = mix(uNight, uDay, daymix);
  // faint graticule every 15 degrees
  float latDeg = degrees(asin(clamp(n.y,-1.0,1.0)));
  float lonDeg = degrees(atan(n.x, n.z));
  float gLat = abs(fract(latDeg/15.0 + 0.5) - 0.5);
  float gLon = abs(fract(lonDeg/15.0 + 0.5) - 0.5);
  float grid = 1.0 - smoothstep(0.0, 0.02, min(gLat, gLon));
  col += grid * 0.05;
  // limb shading
  col *= 0.7 + 0.3 * clamp(vRotZ, 0.0, 1.0);
  frag = vec4(col, 1.0);
}`;

const FILL_VS = `#version 300 es
precision highp float;
in vec3 aPos; in float aIdx;
uniform mat4 uModel; uniform vec2 uScale;
uniform sampler2D uColors; uniform int uSide;
flat out vec4 vColor; out vec3 vObj;
void main(){
  int i = int(aIdx + 0.5);
  vColor = texelFetch(uColors, ivec2(i % uSide, i / uSide), 0);
  vObj = aPos;
  vec4 r = uModel * vec4(aPos,1.0);
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5 - 0.002, 1.0);
}`;

const FILL_FS = `#version 300 es
precision highp float;
flat in vec4 vColor; in vec3 vObj;
uniform vec3 uSun;
out vec4 frag;
void main(){
  float conf = vColor.a;
  vec3 col = vColor.rgb;
  float alpha = (conf < 0.3) ? 0.28 : 0.94;        // faint when no value, solid otherwise
  float day = clamp(dot(normalize(vObj), uSun) * 1.6 + 0.85, 0.45, 1.0);
  frag = vec4(col * day, alpha);
}`;

const LINE_VS = `#version 300 es
precision highp float;
in vec3 aPos;
uniform mat4 uModel; uniform vec2 uScale; uniform float uZOff;
void main(){
  vec4 r = uModel * vec4(aPos,1.0);
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5 - uZOff, 1.0);
}`;

const LINE_FS = `#version 300 es
precision highp float;
uniform vec4 uColor;
out vec4 frag;
void main(){ frag = uColor; }`;

const POINT_VS = `#version 300 es
precision highp float;
in vec3 aPos; in float aW;
uniform mat4 uModel; uniform vec2 uScale; uniform float uPx;
out float vNight; out float vW;
void main(){
  vec4 r = uModel * vec4(aPos,1.0);
  vNight = 1.0; vW = aW;
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5 - 0.004, 1.0);
  gl_PointSize = uPx * (0.8 + aW * 0.5);
}`;

const CITY_VS = `#version 300 es
precision highp float;
in vec3 aPos; in float aW;
uniform mat4 uModel; uniform vec2 uScale; uniform vec3 uSun; uniform float uPx;
out float vNight; out float vW;
void main(){
  vec3 n = normalize(aPos);
  vNight = smoothstep(0.10, -0.18, dot(n, uSun));   // only glow at night
  vW = aW;
  vec4 r = uModel * vec4(aPos,1.0);
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5 - 0.003, 1.0);
  gl_PointSize = uPx * (0.7 + aW * 0.6);
}`;

const POINT_FS = `#version 300 es
precision highp float;
in float vNight; in float vW;
uniform vec4 uColor;
out vec4 frag;
void main(){
  vec2 d = gl_PointCoord - 0.5;
  float fall = 1.0 - smoothstep(0.15, 0.5, length(d));
  float a = uColor.a * fall * vNight;
  if (a < 0.01) discard;
  frag = vec4(uColor.rgb, a);
}`;

const THREAD_VS = `#version 300 es
precision highp float;
in vec3 aPos; in float aT;
uniform mat4 uModel; uniform vec2 uScale;
out float vT;
void main(){
  vT = aT;
  vec4 r = uModel * vec4(aPos,1.0);
  gl_Position = vec4(r.x*uScale.x, r.y*uScale.y, -r.z*0.5 - 0.005, 1.0);
}`;

const THREAD_FS = `#version 300 es
precision highp float;
in float vT;
uniform vec4 uColor; uniform float uProgress; uniform float uFade;
out vec4 frag;
void main(){
  float head = exp(-42.0 * abs(vT - uProgress));
  float trail = (vT < uProgress) ? 0.22 : 0.0;
  float a = (trail + head) * uFade;
  if (a < 0.01) discard;
  frag = vec4(uColor.rgb, a * uColor.a);
}`;

/* ---------------- helpers ---------------- */

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    throw new Error('shader: ' + gl.getShaderInfoLog(sh));
  }
  return sh;
}
function program(gl, vs, fs) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error('link: ' + gl.getProgramInfoLog(p));
  return p;
}

/** Subsolar point (lat/lon, degrees) from real UTC — declination approximation
    plus hour-angle from clock time; good to well under a degree of terminator. */
export function subsolarPoint(date = new Date()) {
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  const doy = (date.getTime() - start) / 86400000;
  const decl = -23.44 * Math.cos(((2 * Math.PI) / 365.24) * (doy + 10));
  const utcH = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
  const lon = (12 - utcH) * 15; // solar noon meridian
  return [decl, lon];
}

const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

/** Build fill + line vertex data for a FeatureCollection layer. */
function buildLayerMesh(features, fillR, lineR) {
  const pos = [], idx = [], tris = [], linePos = [];
  for (let f = 0; f < features.length; f++) {
    eachPolygon(features[f].geometry, (rawRing) => {
      const ring = normalizeRing(rawRing);
      const base = pos.length / 3;
      for (const [lon, lat] of ring) {
        const p = llToXyz(lat, lon, fillR);
        pos.push(p[0], p[1], p[2]);
        idx.push(f);
      }
      const t = earClip(ring);
      for (const i of t) tris.push(base + i);
      // boundary line segments
      for (let i = 0; i < ring.length; i++) {
        const a = ring[i], b = ring[(i + 1) % ring.length];
        const pa = llToXyz(a[1], a[0], lineR), pb = llToXyz(b[1], b[0], lineR);
        linePos.push(pa[0], pa[1], pa[2], pb[0], pb[1], pb[2]);
      }
    });
  }
  return {
    pos: new Float32Array(pos),
    idx: new Float32Array(idx),
    tris: new Uint32Array(tris),
    linePos: new Float32Array(linePos),
  };
}

/* ---------------- the globe ---------------- */

export class Tier1Globe {
  /**
   * @param {HTMLElement} container
   * @param {object} statesGeo GeoJSON FeatureCollection (52 states)
   * @param {{onPick:Function,onHover:Function,onNeedCounties:Function,onLodChange:Function}} hooks
   */
  constructor(container, statesGeo, hooks = {}) {
    this.container = container;
    this.hooks = hooks;
    this.canvas = document.createElement('canvas');
    container.appendChild(this.canvas);
    const gl = this.canvas.getContext('webgl2', { antialias: true, preserveDrawingBuffer: true });
    if (!gl) { this.canvas.remove(); throw new Error('WebGL2 unavailable'); }
    this.gl = gl;

    // camera
    this.cam = { lat: 39, lon: -97.5, zoom: 2.05 };
    this.flight = null;
    this.targetZoom = null;                  // eased wheel-zoom target (null = settled)
    this._keys = new Set();                  // currently-held nav keys
    this._navRaf = null;                     // rAF handle, live ONLY while a key is held

    // layers
    this.states = { features: statesGeo.features, info: statesGeo.features.map(featureInfo) };
    this.counties = null;          // set lazily
    this.countiesRequested = false;
    this.districtsGeo = null;
    this.districtsVisible = false;
    this.showCounties = false;

    this.choropleth = { tier: 'state', values: null, confidence: null, rampType: 'sequential', min: 0, max: 1 };
    this.override = null;          // {tier, colors:Map(key -> [r,g,b])} — builder / election night
    this.threads = [];             // {buf,tBuf,n,birth}
    this.pins = null;

    this._initGL();
    this._buildStatic();
    this.refreshTheme();
    this.applyColors();

    this._bindEvents();
    this.resize();
    this._raf = requestAnimationFrame((t) => this._frame(t));
  }

  /* ----- GL setup ----- */

  _initGL() {
    const gl = this.gl;
    this.progs = {
      sphere: program(gl, SPHERE_VS, SPHERE_FS),
      fill: program(gl, FILL_VS, FILL_FS),
      line: program(gl, LINE_VS, LINE_FS),
      city: program(gl, CITY_VS, POINT_FS),
      pin: program(gl, POINT_VS, POINT_FS),
      thread: program(gl, THREAD_VS, THREAD_FS),
    };
    gl.enable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  }

  _buffer(data, target = this.gl.ARRAY_BUFFER) {
    const gl = this.gl;
    const b = gl.createBuffer();
    gl.bindBuffer(target, b);
    gl.bufferData(target, data, gl.STATIC_DRAW);
    return b;
  }

  _buildStatic() {
    const gl = this.gl;
    // sphere mesh 48 x 96
    const LA = 48, LO = 96, sp = [], si = [];
    for (let i = 0; i <= LA; i++) {
      const lat = -90 + (180 * i) / LA;
      for (let j = 0; j <= LO; j++) {
        const lon = -180 + (360 * j) / LO;
        const p = llToXyz(lat, lon, 1);
        sp.push(p[0], p[1], p[2]);
      }
    }
    for (let i = 0; i < LA; i++) for (let j = 0; j < LO; j++) {
      const a = i * (LO + 1) + j, b = a + LO + 1;
      si.push(a, b, a + 1, a + 1, b, b + 1);
    }
    this.sphere = { pos: this._buffer(new Float32Array(sp)), idx: this._buffer(new Uint32Array(si), gl.ELEMENT_ARRAY_BUFFER), n: si.length };

    // state layer
    const m = buildLayerMesh(this.states.features, 1.0015, 1.003);
    this.stateMesh = this._uploadLayer(m, this.states.features.length);

    // city lights
    const cp = [], cw = [];
    for (const [lon, lat, w] of CITY_LIGHTS) {
      const p = llToXyz(lat, lon, 1.002);
      cp.push(p[0], p[1], p[2]); cw.push(w);
    }
    this.cities = { pos: this._buffer(new Float32Array(cp)), w: this._buffer(new Float32Array(cw)), n: cw.length };
  }

  _uploadLayer(mesh, nFeatures) {
    const gl = this.gl;
    const side = Math.max(2, Math.ceil(Math.sqrt(nFeatures)));
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, side, side, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(side * side * 4));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    return {
      pos: this._buffer(mesh.pos),
      idx: this._buffer(mesh.idx),
      tris: this._buffer(mesh.tris, gl.ELEMENT_ARRAY_BUFFER),
      nTris: mesh.tris.length,
      lines: this._buffer(mesh.linePos),
      nLines: mesh.linePos.length / 3,
      tex, side, nFeatures,
    };
  }

  /* ----- data & colors ----- */

  setCountiesGeo(geo) {
    if (!geo || this.counties) return;
    this.counties = { features: geo.features, info: geo.features.map(featureInfo) };
    // heavy: build mesh in chunks off the interaction path
    const features = geo.features;
    const self = this;
    let i = 0;
    const partial = { pos: [], idx: [], tris: [], linePos: [] };
    function chunk() {
      const end = Math.min(features.length, i + 250);
      const m = buildLayerMesh(features.slice(i, end), 1.001, 1.0025);
      const base = partial.pos.length / 3;
      for (const v of m.pos) partial.pos.push(v);
      for (const v of m.idx) partial.idx.push(v + i);
      for (const v of m.tris) partial.tris.push(v + base);
      for (const v of m.linePos) partial.linePos.push(v);
      i = end;
      if (i < features.length) setTimeout(chunk, 0);
      else {
        self.countyMesh = self._uploadLayer({
          pos: new Float32Array(partial.pos),
          idx: new Float32Array(partial.idx),
          tris: new Uint32Array(partial.tris),
          linePos: new Float32Array(partial.linePos),
        }, features.length);
        self.applyColors();
      }
    }
    setTimeout(chunk, 0);
  }

  setDistrictsGeo(geo) {
    this.districtsGeo = geo;
    if (geo) {
      const m = buildLayerMesh(geo.features, 1.0018, 1.0035);
      this.districtMesh = this._uploadLayer(m, geo.features.length);
      this.applyColors(); // color the fresh mesh now (setCountiesGeo does the same)
    }
  }
  setDistrictsVisible(v) { this.districtsVisible = !!v; }

  setChoropleth(cfg) {
    this.choropleth = { ...this.choropleth, ...cfg };
    this.applyColors();
  }

  /** Builder / election-night override: map of unit key → [r,g,b] (0-255), or null. */
  setOverrideColors(tier, colors) {
    this.override = colors ? { tier, colors } : null;
    this.applyColors();
  }

  setPins(pins) {
    const gl = this.gl;
    this.pins = null;
    if (!pins || !pins.length) return;
    const pp = [], pw = [];
    for (const p of pins) {
      const v = llToXyz(p.lat, p.lon, 1.004);
      pp.push(v[0], v[1], v[2]); pw.push(p.kind === 'call' ? 3 : p.kind === 'story' ? 2 : 1);
    }
    this.pins = { pos: this._buffer(new Float32Array(pp)), w: this._buffer(new Float32Array(pw)), n: pw.length };
  }

  refreshTheme() {
    this.palette = themePalette();
    this.applyColors();
  }

  /** Recompute both layers' color textures from choropleth/override state. */
  applyColors() {
    this._paintLayer(this.stateMesh, this.states.features, (f) => f.id);
    if (this.countyMesh) this._paintLayer(this.countyMesh, this.counties.features, (f) => f.id);
    if (this.districtMesh && this.districtsGeo) {
      this._paintLayer(this.districtMesh, this.districtsGeo.features, (f) => f.id || f.properties?.GEOID);
    }
  }

  _paintLayer(mesh, features, keyOf) {
    if (!mesh) return;
    const gl = this.gl;
    const { side } = mesh;
    const data = new Uint8Array(side * side * 4);
    const pal = this.palette;
    const ch = this.choropleth;
    const layerTier = features === this.states.features ? 'state'
      : this.counties && features === this.counties.features ? 'county' : 'district';
    const base = pal.low;
    for (let i = 0; i < features.length; i++) {
      const key = keyOf(features[i]);
      let rgb = base, conf = 40; // default: faint no-value fill
      if (this.override && this.override.tier === layerTier && this.override.colors.has(key)) {
        rgb = this.override.colors.get(key); conf = 255;
      } else if (ch.values && ch.tier === layerTier && ch.values[key] !== undefined) {
        const span = (ch.max - ch.min) || 1;
        const t = (ch.values[key] - ch.min) / span;
        rgb = rampColor(ch.rampType === 'diverging' ? 1 - t : t, ch.rampType, pal);
        conf = 255; // every value renders solid — no confidence hatching
      }
      data[i * 4] = rgb[0]; data[i * 4 + 1] = rgb[1]; data[i * 4 + 2] = rgb[2]; data[i * 4 + 3] = conf;
    }
    gl.bindTexture(gl.TEXTURE_2D, mesh.tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, side, side, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
  }

  /* ----- camera ----- */

  flyTo(lat, lon, zoom, ms = 1100) {
    this.targetZoom = null; // fly-to owns the camera; drop any pending wheel-zoom
    this.flight = {
      from: { ...this.cam },
      to: { lat, lon: this._nearLon(lon), zoom: zoom ?? this.cam.zoom },
      t0: performance.now(), dur: ms,
    };
  }

  flyToFeature(tier, key, pad = 1) {
    const layer = tier === 'county' ? this.counties : this.states;
    if (!layer) return;
    const i = layer.features.findIndex((f) => f.id === key);
    if (i < 0) return;
    const { bbox, centroid } = layer.info[i];
    const span = Math.max(bbox[2] - bbox[0], (bbox[3] - bbox[1]) * 1.6, 2) * pad;
    const zoom = Math.min(24, Math.max(1.6, 95 / span));
    this.flyTo(centroid[1], centroid[0], zoom);
  }

  _nearLon(lon) { // take the short way around
    while (lon - this.cam.lon > 180) lon -= 360;
    while (lon - this.cam.lon < -180) lon += 360;
    return lon;
  }

  addThread(a, b) {
    const pts = greatCircle(a[0], a[1], b[0], b[1], 64);
    const t = new Float32Array(64);
    for (let i = 0; i < 64; i++) t[i] = i / 63;
    this.threads.push({
      pos: this._buffer(pts), tBuf: this._buffer(t), n: 64, birth: performance.now(),
    });
    if (this.threads.length > 12) this.threads.shift();
  }

  /* ----- interaction ----- */

  _bindEvents() {
    const c = this.canvas;
    this._drag = null;
    this._onDown = (e) => {
      this.targetZoom = null; // a drag takes the camera; drop any pending wheel-zoom
      this._drag = { x: e.clientX, y: e.clientY, lat: this.cam.lat, lon: this.cam.lon, moved: false };
      c.setPointerCapture(e.pointerId);
    };
    this._onMove = (e) => {
      if (this._drag && (e.buttons & 1)) {
        const dx = e.clientX - this._drag.x, dy = e.clientY - this._drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) this._drag.moved = true;
        const degPerPx = 90 / (this.canvas.clientHeight * this.cam.zoom * 0.5);
        this.cam.lon = this._clampLon(this._drag.lon - dx * degPerPx);
        this.cam.lat = Math.max(5, Math.min(74, this._drag.lat + dy * degPerPx));
        this.flight = null;
      } else {
        this._hoverXY = [e.clientX, e.clientY];
      }
    };
    this._onUp = (e) => {
      const wasDrag = this._drag && this._drag.moved;
      this._drag = null;
      if (!wasDrag) {
        const unit = this.pick(e.clientX, e.clientY);
        this.hooks.onPick && this.hooks.onPick(unit, e);
      }
    };
    this._onWheel = (e) => {
      e.preventDefault();
      // set a TARGET zoom eased in _frame; normalize deltaMode across devices
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? (this.canvas.height || 600) : 1;
      const dy = e.deltaY * unit;
      const from = this.targetZoom != null ? this.targetZoom : this.cam.zoom;
      this.targetZoom = Math.max(G_ZOOM_MIN, Math.min(G_ZOOM_MAX, from * Math.exp(-dy * 0.0012)));
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
    this._onKeyUp = (e) => { this._keys.delete(e.key.toLowerCase()); };
    this._onBlur = () => { this._keys.clear(); };
    window.addEventListener('keydown', this._onKeyDown);
    window.addEventListener('keyup', this._onKeyUp);
    window.addEventListener('blur', this._onBlur);
    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
  }

  /* Key-gated rAF: starts on the first keydown, self-stops when every key is
     released — never a permanent loop of its own. WASD/arrows orbit the globe
     (lat/lon), Q/E and +/- zoom, all within the existing camera clamps. */
  _startNav() {
    if (this._navRaf != null) return;
    this._navLast = performance.now();
    this._navRaf = requestAnimationFrame((t) => this._navFrame(t));
  }

  _navFrame(now) {
    if (!this._keys.size) { this._navRaf = null; return; }
    const dt = Math.min(0.05, (now - (this._navLast || now)) / 1000);
    this._navLast = now;
    const has = (k) => this._keys.has(k);
    // pan slows as you zoom in, so it feels the same on screen
    const panDeg = (70 / this.cam.zoom) * dt * 60 * 0.5;
    let dLon = 0, dLat = 0;
    if (has('a') || has('arrowleft')) dLon -= 1;
    if (has('d') || has('arrowright')) dLon += 1;
    if (has('w') || has('arrowup')) dLat += 1;
    if (has('s') || has('arrowdown')) dLat -= 1;
    if (dLon) { this.cam.lon = this._clampLon(this.cam.lon + dLon * panDeg); this.flight = null; }
    if (dLat) { this.cam.lat = Math.max(5, Math.min(74, this.cam.lat + dLat * panDeg)); this.flight = null; }
    let zf = 0;
    if (has('e') || has('+') || has('=')) zf += 1;
    if (has('q') || has('-') || has('_')) zf -= 1;
    if (zf) { this.cam.zoom = Math.max(G_ZOOM_MIN, Math.min(G_ZOOM_MAX, this.cam.zoom * Math.exp(zf * 1.6 * dt))); this.flight = null; this.targetZoom = null; }
    this._navRaf = requestAnimationFrame((t) => this._navFrame(t));
  }

  _clampLon(lon) { return Math.max(-179, Math.min(-45, lon)); }

  /** Screen → unit (see PICKING note in the file header). Returns
      {tier:'state'|'county', key, name} or null. */
  pick(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left, y = clientY - rect.top;
    const aspect = rect.width / rect.height;
    const xnd = (2 * x) / rect.width - 1;
    const ynd = 1 - (2 * y) / rect.height;
    const vx = (xnd * aspect) / this.cam.zoom;
    const vy = ynd / this.cam.zoom;
    const rr = vx * vx + vy * vy;
    if (rr > 1) return null;
    const vz = Math.sqrt(1 - rr);
    // inverse rotation: model = rotX(lat)·rotY(-lon) ⇒ inverse = rotY(lon)·rotX(-lat)
    const latR = (this.cam.lat * Math.PI) / 180, lonR = (this.cam.lon * Math.PI) / 180;
    const inv = mat4.multiply(mat4.rotY(lonR), mat4.rotX(-latR));
    const [ox, oy, oz] = mat4.transformVec3(inv, [vx, vy, vz]);
    const lat = (Math.asin(Math.max(-1, Math.min(1, oy))) * 180) / Math.PI;
    const lon = (Math.atan2(ox, oz) * 180) / Math.PI;

    const st = this._findFeature(this.states, lon, lat);
    if (!st) return null;
    if (this.showCounties && this.counties) {
      const stateFips = st.feature.id;
      const cands = [];
      for (let i = 0; i < this.counties.features.length; i++) {
        const f = this.counties.features[i];
        if (f.properties.STATE !== stateFips) continue;
        cands.push(i);
      }
      for (const i of cands) {
        const f = this.counties.features[i];
        const bb = this.counties.info[i].bbox;
        if (lon < bb[0] || lon > bb[2] || lat < bb[1] || lat > bb[3]) continue;
        if (pointInFeature(lon, lat, f)) {
          return { tier: 'county', key: f.id, name: `${f.properties.NAME} ${f.properties.LSAD || ''}`.trim(), stateFips, lat, lon };
        }
      }
    }
    return { tier: 'state', key: st.feature.id, name: st.feature.properties.name, lat, lon };
  }

  _findFeature(layer, lon, lat) {
    for (let i = 0; i < layer.features.length; i++) {
      const bb = layer.info[i].bbox;
      // bbox may live in the antimeridian-shifted domain (Alaska)
      const testLon = bb[2] > 180 && lon < 0 ? lon + 360 : lon;
      if (testLon < bb[0] - 0.01 || testLon > bb[2] + 0.01 || lat < bb[1] - 0.01 || lat > bb[3] + 0.01) continue;
      if (pointInFeature(lon, lat, layer.features[i])) return { feature: layer.features[i], i };
    }
    return null;
  }

  /* ----- render loop ----- */

  resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = this.container.clientWidth || 800, h = this.container.clientHeight || 600;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
  }

  _frame(now) {
    this._raf = requestAnimationFrame((t) => this._frame(t));
    // fly-to easing
    if (this.flight) {
      const f = this.flight;
      const t = Math.min(1, (now - f.t0) / f.dur);
      const e = ease(t);
      this.cam.lat = f.from.lat + (f.to.lat - f.from.lat) * e;
      this.cam.lon = f.from.lon + (f.to.lon - f.from.lon) * e;
      this.cam.zoom = f.from.zoom + (f.to.zoom - f.from.zoom) * e;
      if (t >= 1) this.flight = null;
    }
    // eased wheel zoom toward targetZoom
    if (this.targetZoom != null) {
      let z = this.cam.zoom + (this.targetZoom - this.cam.zoom) * 0.22;
      if (Math.abs(this.targetZoom - z) < 0.002) z = this.targetZoom;
      this.cam.zoom = z;
      if (this.cam.zoom === this.targetZoom) this.targetZoom = null;
    }
    // LOD
    const wantCounties = this.cam.zoom >= COUNTY_LOD_ZOOM;
    if (wantCounties && !this.countiesRequested) {
      this.countiesRequested = true;
      this.hooks.onNeedCounties && this.hooks.onNeedCounties();
    }
    if (wantCounties !== this.showCounties) {
      this.showCounties = wantCounties;
      this.hooks.onLodChange && this.hooks.onLodChange(wantCounties ? 'county' : 'state');
    }
    // hover (throttled to the frame)
    if (this._hoverXY && this.hooks.onHover) {
      const [hx, hy] = this._hoverXY;
      this._hoverXY = null;
      this.hooks.onHover(this.pick(hx, hy), hx, hy);
    }
    this._draw(now);
  }

  _draw(now) {
    const gl = this.gl;
    const pal = this.palette;
    const bg = pal.bg.map((v) => v / 255);
    gl.clearColor(bg[0], bg[1], bg[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    const rect = this.canvas;
    const aspect = rect.width / rect.height;
    const scale = [this.cam.zoom / aspect, this.cam.zoom];
    const latR = (this.cam.lat * Math.PI) / 180, lonR = (this.cam.lon * Math.PI) / 180;
    const model = mat4.multiply(mat4.rotX(latR), mat4.rotY(-lonR));
    const [sLat, sLon] = subsolarPoint();
    const sun = llToXyz(sLat, sLon, 1);

    const setCommon = (p) => {
      gl.useProgram(p);
      gl.uniformMatrix4fv(gl.getUniformLocation(p, 'uModel'), false, model);
      gl.uniform2fv(gl.getUniformLocation(p, 'uScale'), scale);
    };
    const bindAttr = (p, name, buf, size) => {
      const loc = gl.getAttribLocation(p, name);
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
    };

    // sphere (dim earth + terminator)
    {
      const p = this.progs.sphere;
      setCommon(p);
      gl.uniform3fv(gl.getUniformLocation(p, 'uSun'), sun);
      const day = pal.light
        ? [0.93, 0.92, 0.89].map((v, i) => v * 0.92 + (pal.accent[i] / 255) * 0.05)
        : pal.bg.map((v, i) => v / 255 + (pal.accent[i] / 255) * 0.10 + 0.02);
      const night = pal.light ? day.map((v) => v * 0.55) : pal.bg.map((v) => (v / 255) * 0.45);
      gl.uniform3fv(gl.getUniformLocation(p, 'uDay'), day);
      gl.uniform3fv(gl.getUniformLocation(p, 'uNight'), night);
      bindAttr(p, 'aPos', this.sphere.pos, 3);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.sphere.idx);
      gl.drawElements(gl.TRIANGLES, this.sphere.n, gl.UNSIGNED_INT, 0);
    }

    // fills
    const drawFill = (mesh) => {
      const p = this.progs.fill;
      setCommon(p);
      gl.uniform3fv(gl.getUniformLocation(p, 'uSun'), sun);
      gl.uniform1i(gl.getUniformLocation(p, 'uSide'), mesh.side);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, mesh.tex);
      gl.uniform1i(gl.getUniformLocation(p, 'uColors'), 0);
      bindAttr(p, 'aPos', mesh.pos, 3);
      bindAttr(p, 'aIdx', mesh.idx, 1);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.tris);
      gl.drawElements(gl.TRIANGLES, mesh.nTris, gl.UNSIGNED_INT, 0);
    };
    const drawLines = (mesh, color, zOff) => {
      const p = this.progs.line;
      setCommon(p);
      gl.uniform4fv(gl.getUniformLocation(p, 'uColor'), color);
      gl.uniform1f(gl.getUniformLocation(p, 'uZOff'), zOff);
      bindAttr(p, 'aPos', mesh.lines, 3);
      gl.drawArrays(gl.LINES, 0, mesh.nLines);
    };

    if (this.choropleth.tier === 'district' && this.districtMesh) {
      // House mode: fill congressional districts, keep state outlines for context
      drawFill(this.districtMesh);
      drawLines(this.stateMesh, [pal.accent[0] / 255, pal.accent[1] / 255, pal.accent[2] / 255, 0.7], 0.004);
    } else if (this.showCounties && this.countyMesh) {
      drawFill(this.countyMesh);
      drawLines(this.countyMesh, [pal.line[0] / 255, pal.line[1] / 255, pal.line[2] / 255, 0.5], 0.003);
      drawLines(this.stateMesh, [pal.accent[0] / 255, pal.accent[1] / 255, pal.accent[2] / 255, 0.85], 0.004);
    } else {
      drawFill(this.stateMesh);
      drawLines(this.stateMesh, [pal.accent[0] / 255, pal.accent[1] / 255, pal.accent[2] / 255, 0.7], 0.004);
    }
    if (this.districtsVisible && this.districtMesh) {
      drawLines(this.districtMesh, [pal.text[0] / 255, pal.text[1] / 255, pal.text[2] / 255, 0.55], 0.0045);
    }

    // city lights (night side only)
    {
      const p = this.progs.city;
      setCommon(p);
      gl.uniform3fv(gl.getUniformLocation(p, 'uSun'), sun);
      gl.uniform1f(gl.getUniformLocation(p, 'uPx'), 3.2 * (this.canvas.width / 1400) * Math.sqrt(this.cam.zoom));
      gl.uniform4f(gl.getUniformLocation(p, 'uColor'), 1.0, 0.9, 0.6, 0.85);
      bindAttr(p, 'aPos', this.cities.pos, 3);
      bindAttr(p, 'aW', this.cities.w, 1);
      gl.drawArrays(gl.POINTS, 0, this.cities.n);
    }

    // pins
    if (this.pins) {
      const p = this.progs.pin;
      setCommon(p);
      gl.uniform1f(gl.getUniformLocation(p, 'uPx'), 6);
      gl.uniform4f(gl.getUniformLocation(p, 'uColor'), pal.accent[0] / 255, pal.accent[1] / 255, pal.accent[2] / 255, 0.95);
      bindAttr(p, 'aPos', this.pins.pos, 3);
      bindAttr(p, 'aW', this.pins.w, 1);
      gl.drawArrays(gl.POINTS, 0, this.pins.n);
    }

    // correlation threads
    if (this.threads.length) {
      const p = this.progs.thread;
      setCommon(p);
      gl.uniform4f(gl.getUniformLocation(p, 'uColor'), pal.accent[0] / 255, pal.accent[1] / 255, pal.accent[2] / 255, 1.0);
      gl.disable(gl.DEPTH_TEST);
      const dead = [];
      for (const th of this.threads) {
        const age = (now - th.birth) / 1000;
        if (age > 5.5) { dead.push(th); continue; }
        const progress = Math.min(1, age / 1.8);
        const fade = age < 4 ? 1 : 1 - (age - 4) / 1.5;
        gl.uniform1f(gl.getUniformLocation(p, 'uProgress'), progress);
        gl.uniform1f(gl.getUniformLocation(p, 'uFade'), Math.max(0, fade));
        bindAttr(p, 'aPos', th.pos, 3);
        bindAttr(p, 'aT', th.tBuf, 1);
        gl.drawArrays(gl.LINE_STRIP, 0, th.n);
      }
      for (const d of dead) this.threads.splice(this.threads.indexOf(d), 1);
      gl.enable(gl.DEPTH_TEST);
    }
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
