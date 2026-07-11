/* anim.js — tiny motion utilities (zero-dependency, stdlib-of-the-browser).
   animateNumber: count-up ticks for poll-average numbers and builder
   toplines. skeleton: shimmering placeholder blocks (pure CSS animation,
   see .skel in styles.css) shown while a fetch is in flight — replaces
   bare "loading…" text. Both respect prefers-reduced-motion. */

function reducedMotion() {
  try { return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; }
  catch (e) { return false; }
}

/**
 * Animate el.textContent from `from` to `to` over `ms` (ease-out cubic).
 * @param {HTMLElement} el
 * @param {number} from
 * @param {number} to
 * @param {number} ms
 * @param {(v:number)=>string} [fmt] formatter; defaults to integers when both
 *   ends are integers, otherwise one decimal place.
 */
export function animateNumber(el, from, to, ms = 500, fmt = null) {
  if (!el) return;
  from = Number.isFinite(Number(from)) ? Number(from) : 0;
  to = Number.isFinite(Number(to)) ? Number(to) : 0;
  const ints = Number.isInteger(from) && Number.isInteger(to);
  const f = fmt || ((v) => (ints ? Math.round(v).toLocaleString() : v.toFixed(1)));
  if (reducedMotion() || from === to || ms <= 0) { el.textContent = f(to); return; }
  const t0 = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - t0) / ms);
    const e = 1 - Math.pow(1 - t, 3); // ease-out cubic
    el.textContent = f(from + (to - from) * e);
    if (t < 1 && el.isConnected) requestAnimationFrame(tick);
    else if (t >= 1) el.textContent = f(to);
  };
  requestAnimationFrame(tick);
}

/**
 * Build a skeleton-loader block: `rows` shimmering bars of varied width.
 * @param {number} rows
 * @param {{table?:boolean}} opts table=true renders a header bar + uniform rows
 * @returns {HTMLElement}
 */
export function skeleton(rows = 3, opts = {}) {
  const box = document.createElement('div');
  box.className = 'skel-group';
  box.setAttribute('aria-hidden', 'true');
  if (opts.table) {
    const head = document.createElement('div');
    head.className = 'skel skel-bar';
    head.style.width = '100%';
    head.style.height = '14px';
    box.appendChild(head);
    for (let i = 0; i < rows; i++) {
      const r = document.createElement('div');
      r.className = 'skel skel-bar';
      r.style.width = `${88 + ((i * 7) % 12)}%`;
      box.appendChild(r);
    }
    return box;
  }
  const widths = [92, 64, 78, 55, 84, 47];
  for (let i = 0; i < rows; i++) {
    const r = document.createElement('div');
    r.className = 'skel skel-bar';
    r.style.width = `${widths[i % widths.length]}%`;
    box.appendChild(r);
  }
  return box;
}
