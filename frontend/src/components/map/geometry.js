/* geometry.js — shared geometry for the map tiers: ear-clipping triangulation
   (2D lon/lat space, per polygon), spherical conversions, minimal mat4 math,
   great-circle sampling, Albers conic equal-area (for the Tier-2 composite),
   point-in-polygon picking, and antimeridian handling for Alaska. */

const D2R = Math.PI / 180;

/** lat/lon (degrees) → unit-sphere xyz. lon 0 faces +z so the default camera
    convention in Tier1Globe is a plain rotX(lat)·rotY(-lon). */
export function llToXyz(lat, lon, r = 1) {
  const la = lat * D2R, lo = lon * D2R;
  return [r * Math.cos(la) * Math.sin(lo), r * Math.sin(la), r * Math.cos(la) * Math.cos(lo)];
}

/* ---------- antimeridian ----------
   A ring whose longitudes span more than 180° (Alaska's Aleutians) can't be
   triangulated or point-tested in raw lon/lat space; shift negative lons +360
   into a continuous domain. The shift only affects 2D math — vertex positions
   on the sphere are computed from the original lon, which is congruent. */
export function normalizeRing(ring) {
  let min = Infinity, max = -Infinity;
  for (const [lon] of ring) { if (lon < min) min = lon; if (lon > max) max = lon; }
  if (max - min <= 180) return ring;
  return ring.map(([lon, lat]) => [lon < 0 ? lon + 360 : lon, lat]);
}

/* ---------- ear clipping ----------
   O(n²) ear clipping on the outer ring of each polygon (holes are absent from
   the vendored state/county data at this simplification level). Handles the
   multi-polygon states (MI, AK, HI) because callers triangulate per-polygon.
   Returns index triples into the ring. */
export function earClip(ring) {
  // drop duplicate closing point
  let pts = ring;
  const n0 = pts.length;
  if (n0 > 1 && pts[0][0] === pts[n0 - 1][0] && pts[0][1] === pts[n0 - 1][1]) pts = pts.slice(0, -1);
  const n = pts.length;
  if (n < 3) return [];

  // signed area — ensure counter-clockwise winding
  let area = 0;
  for (let i = 0, j = n - 1; i < n; j = i++) area += (pts[j][0] * pts[i][1] - pts[i][0] * pts[j][1]);
  const ccw = area > 0;

  const idx = [];
  for (let i = 0; i < n; i++) idx.push(ccw ? i : n - 1 - i);

  const cross = (a, b, c) =>
    (pts[b][0] - pts[a][0]) * (pts[c][1] - pts[a][1]) - (pts[b][1] - pts[a][1]) * (pts[c][0] - pts[a][0]);
  const inTri = (p, a, b, c) => {
    const [px, py] = pts[p];
    const s1 = (pts[b][0] - pts[a][0]) * (py - pts[a][1]) - (pts[b][1] - pts[a][1]) * (px - pts[a][0]);
    const s2 = (pts[c][0] - pts[b][0]) * (py - pts[b][1]) - (pts[c][1] - pts[b][1]) * (px - pts[b][0]);
    const s3 = (pts[a][0] - pts[c][0]) * (py - pts[c][1]) - (pts[a][1] - pts[c][1]) * (px - pts[c][0]);
    return s1 >= 0 && s2 >= 0 && s3 >= 0;
  };

  const tris = [];
  let guard = 0;
  while (idx.length > 3 && guard < 20000) {
    guard++;
    let clipped = false;
    for (let i = 0; i < idx.length; i++) {
      const a = idx[(i + idx.length - 1) % idx.length], b = idx[i], c = idx[(i + 1) % idx.length];
      if (cross(a, b, c) <= 0) continue;      // reflex vertex, not an ear
      let anyInside = false;
      for (const p of idx) {
        if (p === a || p === b || p === c) continue;
        if (inTri(p, a, b, c)) { anyInside = true; break; }
      }
      if (anyInside) continue;
      tris.push(a, b, c);
      idx.splice(i, 1);
      clipped = true;
      break;
    }
    if (!clipped) { // degenerate remainder (collinear runs) — fan what's left
      for (let i = 1; i < idx.length - 1; i++) tris.push(idx[0], idx[i], idx[i + 1]);
      return tris;
    }
  }
  if (idx.length === 3) tris.push(idx[0], idx[1], idx[2]);
  return tris;
}

/** Iterate a GeoJSON geometry's polygons: cb(outerRing) per polygon. */
export function eachPolygon(geometry, cb) {
  if (!geometry) return;
  if (geometry.type === 'Polygon') cb(geometry.coordinates[0]);
  else if (geometry.type === 'MultiPolygon') for (const poly of geometry.coordinates) cb(poly[0]);
}

/** Feature bbox + a cheap interior point (largest ring's centroid). */
export function featureInfo(feature) {
  let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
  let best = null, bestLen = -1;
  eachPolygon(feature.geometry, (ring) => {
    const r = normalizeRing(ring);
    if (r.length > bestLen) { bestLen = r.length; best = r; }
    for (const [lon, lat] of r) {
      if (lon < minLon) minLon = lon; if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat; if (lat > maxLat) maxLat = lat;
    }
  });
  let cx = 0, cy = 0;
  if (best) { for (const [x, y] of best) { cx += x; cy += y; } cx /= best.length; cy /= best.length; }
  if (cx > 180) cx -= 360;
  return { bbox: [minLon, minLat, maxLon, maxLat], centroid: [cx, cy] };
}

/** point-in-polygon (ray cast) — ring in [lon,lat], point likewise. */
export function pointInRing(lon, lat, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (((yi > lat) !== (yj > lat)) && (lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi)) inside = !inside;
  }
  return inside;
}

/** Test a lon/lat against a feature (handles antimeridian shift). */
export function pointInFeature(lon, lat, feature) {
  let hit = false;
  eachPolygon(feature.geometry, (ring) => {
    if (hit) return;
    const r = normalizeRing(ring);
    let testLon = lon;
    if (r !== ring && lon < 0) testLon = lon + 360; // shifted domain
    if (pointInRing(testLon, lat, r)) hit = true;
  });
  return hit;
}

/* ---------- minimal mat4 (column-major, WebGL layout) ---------- */
export const mat4 = {
  identity: () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]),
  multiply(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
    return o;
  },
  rotX(t) { const c = Math.cos(t), s = Math.sin(t); return new Float32Array([1,0,0,0, 0,c,s,0, 0,-s,c,0, 0,0,0,1]); },
  rotY(t) { const c = Math.cos(t), s = Math.sin(t); return new Float32Array([c,0,-s,0, 0,1,0,0, s,0,c,0, 0,0,0,1]); },
  scale(x, y, z) { return new Float32Array([x,0,0,0, 0,y,0,0, 0,0,z,0, 0,0,0,1]); },
  transformVec3(m, v) {
    return [
      m[0] * v[0] + m[4] * v[1] + m[8]  * v[2],
      m[1] * v[0] + m[5] * v[1] + m[9]  * v[2],
      m[2] * v[0] + m[6] * v[1] + m[10] * v[2],
    ];
  },
};

/** Sample n points along the great circle between two lat/lon pairs, with an
    altitude bump (for correlation-thread arcs). Returns flat xyz array. */
export function greatCircle(latA, lonA, latB, lonB, n = 48, lift = 0.06) {
  const a = llToXyz(latA, lonA), b = llToXyz(latB, lonB);
  const dot = Math.min(1, Math.max(-1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]));
  const omega = Math.acos(dot) || 1e-6;
  const out = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const t = i / (n - 1);
    const s1 = Math.sin((1 - t) * omega) / Math.sin(omega);
    const s2 = Math.sin(t * omega) / Math.sin(omega);
    const r = 1.004 + lift * Math.sin(Math.PI * t);
    out[i * 3]     = (s1 * a[0] + s2 * b[0]) * r;
    out[i * 3 + 1] = (s1 * a[1] + s2 * b[1]) * r;
    out[i * 3 + 2] = (s1 * a[2] + s2 * b[2]) * r;
  }
  return out;
}

/* ---------- Albers conic equal-area (Tier 2) ----------
   Classic composite "Albers USA": lower-48 on parallels 29.5°/45.5°, with
   Alaska and Hawaii (and, here, Puerto Rico) projected separately then
   translated + scaled into insets — the d3.geoAlbersUsa layout constants. */
export function albersFactory(phi1, phi2, lon0, lat0, scale, tx, ty) {
  const p1 = phi1 * D2R, p2 = phi2 * D2R, l0 = lon0 * D2R, la0 = lat0 * D2R;
  const n = (Math.sin(p1) + Math.sin(p2)) / 2;
  const C = Math.cos(p1) ** 2 + 2 * n * Math.sin(p1);
  const rho0 = Math.sqrt(C - 2 * n * Math.sin(la0)) / n;
  return (lon, lat) => {
    let dl = lon * D2R - l0;
    while (dl > Math.PI) dl -= 2 * Math.PI;
    while (dl < -Math.PI) dl += 2 * Math.PI;
    const rho = Math.sqrt(Math.max(0, C - 2 * n * Math.sin(lat * D2R))) / n;
    const th = n * dl;
    return [tx + scale * rho * Math.sin(th), ty - scale * (rho0 - rho * Math.cos(th))];
  };
}

/** Composite Albers-USA projection into a 960×600 reference frame. */
export function makeAlbersUsa() {
  const k = 1070, cx = 480, cy = 250;
  const lower48 = albersFactory(29.5, 45.5, -96.6, 38.7, k, cx, cy);
  const alaska  = albersFactory(55, 65, -156, 60.5, k * 0.35, cx - 0.28 * k, cy + 0.21 * k);
  const hawaii  = albersFactory(8, 18, -157.5, 20.5, k, cx - 0.19 * k, cy + 0.212 * k);
  const pr      = albersFactory(8, 18, -66.4, 18.2, k, cx + 0.36 * k, cy + 0.21 * k);
  return (lon, lat) => {
    // Hawaii threshold is lat < 30 (not 25): HI-02 includes the Northwestern
    // Hawaiian Islands up to ~28.5°N — at lat < 25 those points routed to the
    // mainland projection while the main islands routed to the inset, drawing a
    // line between them. lat<30 & lon<-140 is open Pacific, only Hawaii is there.
    if (lat > 50 && (lon < -128 || lon > 165)) return alaska(lon, lat);
    if (lat < 30 && lon < -140) return hawaii(lon, lat);
    if (lat < 20 && lon > -70) return pr(lon, lat);
    return lower48(lon, lat);
  };
}

/* ---------- color ramps ---------- */
export function hexToRgb(hex) {
  const h = hex.replace('#', '').trim();
  const v = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  return [parseInt(v.slice(0, 2), 16), parseInt(v.slice(2, 4), 16), parseInt(v.slice(4, 6), 16)];
}
export function mix(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }
export function rgbCss(c) { return `rgb(${c[0]},${c[1]},${c[2]})`; }

/** t∈[0,1] on a sequential (bg→accent) or diverging (dem→neutral→rep) ramp. */
export function rampColor(t, rampType, palette) {
  t = Math.max(0, Math.min(1, t));
  if (rampType === 'diverging') {
    // t=0 → strong D, t=0.5 → neutral, t=1 → strong R
    if (t < 0.5) return mix(palette.dem, palette.neutral, t * 2);
    return mix(palette.neutral, palette.rep, (t - 0.5) * 2);
  }
  // sequential: deep-background → accent → slightly brightened accent
  if (t < 0.75) return mix(palette.low, palette.accent, t / 0.75);
  return mix(palette.accent, palette.accentHi, (t - 0.75) / 0.25);
}

/** Read the active theme's palette off computed CSS variables. */
export function themePalette() {
  const cs = getComputedStyle(document.body);
  const get = (name, fallback) => (cs.getPropertyValue(name).trim() || fallback);
  const accent = hexToRgb(get('--accent', '#b08d57'));
  const bg = hexToRgb(get('--bg', '#16181c'));
  const light = bg[0] + bg[1] + bg[2] > 380;
  return {
    accent,
    accentHi: mix(accent, light ? [30, 30, 30] : [255, 255, 255], 0.35),
    low: mix(bg, accent, 0.12),
    bg,
    line: hexToRgb(get('--line', '#33383f')),
    text: hexToRgb(get('--text', '#e9e4d8')),
    dem: hexToRgb(get('--dem', '#4d7dd6')),
    rep: hexToRgb(get('--rep', '#cf5b4e')),
    other: hexToRgb(get('--other', '#7fa06a')),
    // diverging midpoint: a light theme uses near-WHITE (clean 270towin
    // blue→white→red) instead of a muddy bg/text gray; dark themes keep a
    // muted mid-gray that reads against the dark background.
    neutral: light ? [247, 247, 249] : mix(bg, hexToRgb(get('--text', '#e9e4d8')), 0.35),
    light,
  };
}
