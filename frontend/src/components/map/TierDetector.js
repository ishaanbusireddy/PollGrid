/* TierDetector.js — one-shot renderer capability check + manual override.
   Tier 1: WebGL2 globe · Tier 2: 2D-canvas Albers map · Tier 3: list view.
   The saved override ('pollgrid.tier' in localStorage: 'auto'|'1'|'2'|'3')
   wins over detection; App additionally steps down a tier if a tier's
   constructor throws at runtime. */

const KEY = 'pollgrid.tier';

export function getOverride() {
  try { return localStorage.getItem(KEY) || 'auto'; } catch (e) { return 'auto'; }
}

export function setOverride(v) {
  try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ }
}

/** Detect the best supported tier (ignoring override). */
export function detectTier() {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2', { failIfMajorPerformanceCaveat: false });
    if (gl) {
      gl.getExtension('WEBGL_lose_context')?.loseContext();
      return 1;
    }
  } catch (e) { /* fall through */ }
  try {
    const c = document.createElement('canvas');
    if (c.getContext('2d')) return 2;
  } catch (e) { /* fall through */ }
  return 3;
}

/** Final tier choice: override if set, else detection. */
export function chooseTier() {
  const ov = getOverride();
  if (ov === '1' || ov === '2' || ov === '3') return { tier: +ov, source: 'override' };
  return { tier: detectTier(), source: 'detected' };
}
