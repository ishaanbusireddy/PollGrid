/* charts.js — tiny inline-SVG chart helpers (no libraries). All return
   detached SVG elements; colors come from CSS variables via currentColor or
   explicit var() references so charts restyle with the theme. */

const NS = 'http://www.w3.org/2000/svg';

function svg(w, h) {
  const el = document.createElementNS(NS, 'svg');
  el.setAttribute('width', w); el.setAttribute('height', h);
  el.setAttribute('viewBox', `0 0 ${w} ${h}`);
  el.classList.add('mini-svg');
  return el;
}
function node(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/** Simple sparkline. points: number[]; opts: {w,h,stroke} */
export function sparkline(points, opts = {}) {
  const w = opts.w || 140, h = opts.h || 32;
  const el = svg(w, h);
  if (!points || points.length < 2) return el;
  const min = Math.min(...points), max = Math.max(...points);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const d = points.map((v, i) =>
    `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(h - 3 - ((v - min) / span) * (h - 6)).toFixed(1)}`).join(' ');
  el.appendChild(node('path', { d, fill: 'none', stroke: opts.stroke || 'var(--accent)', 'stroke-width': 1.5 }));
  const lastY = h - 3 - ((points[points.length - 1] - min) / span) * (h - 6);
  el.appendChild(node('circle', { cx: w, cy: lastY.toFixed(1), r: 2.2, fill: opts.stroke || 'var(--accent)' }));
  return el;
}

/**
 * Electoral-history trend chart: series [{x:cycleYear, y:marginPct, party}],
 * markers = boundary-redraw cycle years rendered as vertical dashed lines
 * with a <title> tooltip. Positive y = D margin, negative = R.
 */
export function trendChart(series, markers = [], opts = {}) {
  const w = opts.w || 340, h = opts.h || 90;
  const el = svg(w, h);
  if (!series || !series.length) return el;
  const xs = series.map((p) => p.x);
  const minX = Math.min(...xs), maxX = Math.max(...xs), spanX = maxX - minX || 1;
  const maxAbs = Math.max(10, ...series.map((p) => Math.abs(p.y || 0)));
  const X = (x) => 8 + ((x - minX) / spanX) * (w - 16);
  const Y = (y) => h / 2 - (y / maxAbs) * (h / 2 - 10);

  el.appendChild(node('line', { x1: 0, y1: h / 2, x2: w, y2: h / 2, stroke: 'var(--line)', 'stroke-width': 1 }));

  for (const m of markers) {
    const g = node('line', {
      x1: X(m.x), y1: 4, x2: X(m.x), y2: h - 4,
      stroke: 'var(--warn)', 'stroke-width': 1, 'stroke-dasharray': '3,3',
    });
    const t = document.createElementNS(NS, 'title');
    t.textContent = m.label || 'boundary redraw';
    g.appendChild(t);
    el.appendChild(g);
  }

  const d = series.map((p, i) => `${i ? 'L' : 'M'}${X(p.x).toFixed(1)},${Y(p.y || 0).toFixed(1)}`).join(' ');
  el.appendChild(node('path', { d, fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.5 }));

  for (const p of series) {
    const c = node('circle', {
      cx: X(p.x).toFixed(1), cy: Y(p.y || 0).toFixed(1), r: 2.6,
      fill: p.y > 0 ? 'var(--dem)' : p.y < 0 ? 'var(--rep)' : 'var(--other)',
    });
    const t = document.createElementNS(NS, 'title');
    t.textContent = `${p.x}: ${p.y > 0 ? 'D+' + p.y.toFixed(1) : p.y < 0 ? 'R+' + (-p.y).toFixed(1) : 'even'}${p.note ? ' · ' + p.note : ''}`;
    c.appendChild(t);
    el.appendChild(c);
  }
  return el;
}

/** Seat-distribution histogram: dist = {seats: prob}; marker = majority line. */
export function histogram(dist, opts = {}) {
  const w = opts.w || 340, h = opts.h || 80;
  const el = svg(w, h);
  const keys = Object.keys(dist || {}).map(Number).sort((a, b) => a - b);
  if (!keys.length) return el;
  const maxP = Math.max(...keys.map((k) => dist[k]));
  const bw = Math.max(1, (w - 10) / keys.length - 1);
  keys.forEach((k, i) => {
    const bh = (dist[k] / maxP) * (h - 18);
    const isMaj = opts.majority != null && k >= opts.majority;
    const r = node('rect', {
      x: 5 + i * (bw + 1), y: h - 14 - bh, width: bw, height: Math.max(0.5, bh),
      fill: isMaj ? 'var(--dem)' : 'var(--rep)', opacity: 0.85,
    });
    const t = document.createElementNS(NS, 'title');
    t.textContent = `${k} seats: ${(dist[k] * 100).toFixed(1)}%`;
    r.appendChild(t);
    el.appendChild(r);
  });
  const t0 = node('text', { x: 5, y: h - 2, 'font-size': 9, fill: 'var(--muted)' });
  t0.textContent = keys[0];
  const t1 = node('text', { x: w - 5, y: h - 2, 'font-size': 9, fill: 'var(--muted)', 'text-anchor': 'end' });
  t1.textContent = keys[keys.length - 1];
  el.appendChild(t0); el.appendChild(t1);
  return el;
}
