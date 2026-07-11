/* icons.js — tiny inline-SVG glyph set for chips and rows. All glyphs are
   16×16, stroke/fill = currentColor, so they inherit the chip's own color
   (party chips stay --dem/--rep/--other, etc). Text labels are kept next to
   the glyphs wherever clarity demands — icons decorate, never replace. */

const PATHS = {
  // offices
  president: '<path d="M8 1.8l1.9 3.8 4.2.6-3 3 .7 4.2L8 11.4l-3.8 2 .7-4.2-3-3 4.2-.6z" fill="currentColor" stroke="none"/>',
  senate: '<path d="M2 13.5h12M3.2 11.8h9.6M4.2 6.8v5M8 6.8v5M11.8 6.8v5M2.5 6.8h11L8 2.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  house: '<path d="M2 8l6-5.2L14 8M4 7.2v6.3h8V7.2M6.8 13.5V10h2.4v3.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  governor: '<path d="M8 1.8l4.8 1.9v3.8c0 3.3-2 5.6-4.8 6.7-2.8-1.1-4.8-3.4-4.8-6.7V3.7z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  // party (generic pennant — takes the chip's party color)
  party: '<path d="M4 2v12M4 2.5h8l-2 2.7 2 2.7H4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  // confidence
  measured: '<path d="M3 8.5l3.4 3.4L13 4.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  derived: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2.6,2.2"/>',
  // source-reliability tier (signal bars)
  tier: '<path d="M3 13V10M7 13V7M11 13V4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  // influence ledger
  support: '<path d="M8 13V3M4 7l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  oppose: '<path d="M8 3v10M4 9l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  org: '<path d="M3 13.5V4.5l5-2.3 5 2.3v9M5.5 7h1.6M5.5 10h1.6M9 7h1.6M9 10h1.6M2 13.5h12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>',
  money: '<path d="M8 2v12M11 4.5c-.6-1-1.6-1.4-3-1.4-1.7 0-2.8.8-2.8 2.1C5.2 8.4 11 7.4 11 10.5c0 1.4-1.3 2.2-3 2.2-1.5 0-2.6-.5-3.2-1.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  endorse: '<path d="M8 2.2a3.4 3.4 0 100 6.8 3.4 3.4 0 000-6.8zM5.6 8.4L4.6 14 8 12l3.4 2-1-5.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  // provenance
  archival: '<path d="M2 3h12v3H2zM3.2 6v7.5h9.6V6M6.2 8.8h3.6" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>',
};

/**
 * @param {string} name a PATHS key ('president','senate','house','governor',
 *   'party','measured','derived','tier','support','oppose','org','money',
 *   'endorse','archival')
 * @param {string} [title] accessible tooltip; decorative (aria-hidden) if absent
 * @returns {string} inline <svg> HTML, or '' for an unknown name
 */
export function icon(name, title = '') {
  const p = PATHS[name];
  if (!p) return '';
  const t = title ? `<title>${String(title).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))}</title>` : '';
  return `<svg class="icon icon-${name}" viewBox="0 0 16 16" width="12" height="12"${title ? '' : ' aria-hidden="true"'}>${t}${p}</svg>`;
}

/** Office key → icon name (same keys as race_type). */
export function officeIcon(office, title) {
  return PATHS[office] ? icon(office, title) : '';
}
