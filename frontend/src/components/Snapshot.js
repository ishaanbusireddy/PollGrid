/* Snapshot.js — client-side PNG export: crop the live renderer's own canvas,
   composite a title / legend / totals panel onto an offscreen canvas, and
   download. Zero server involvement. Shared by the map snapshot button and
   MapBuilder's export. */

/**
 * @param {HTMLCanvasElement|null} mapCanvas live renderer canvas (null → text-only card)
 * @param {{title:string, subtitle?:string, legend?:Array<{color:string,label:string}>,
 *          totals?:string[], theme?:{bg:string,text:string,accent:string,line:string}}} opts
 */
export function composeSnapshot(mapCanvas, opts = {}) {
  const cs = getComputedStyle(document.body);
  const theme = opts.theme || {
    bg: cs.getPropertyValue('--bg').trim() || '#16181c',
    panel: cs.getPropertyValue('--bg2').trim() || '#1d2026',
    text: cs.getPropertyValue('--text').trim() || '#e9e4d8',
    accent: cs.getPropertyValue('--accent').trim() || '#b08d57',
    line: cs.getPropertyValue('--line').trim() || '#33383f',
    muted: cs.getPropertyValue('--muted').trim() || '#9a958a',
  };

  const W = 1280;
  const mapH = mapCanvas ? Math.round((mapCanvas.height / mapCanvas.width) * W) : 0;
  const panelH = 96 + (opts.totals ? opts.totals.length * 26 : 0);
  const H = mapH + panelH;

  const out = document.createElement('canvas');
  out.width = W; out.height = H;
  const ctx = out.getContext('2d');
  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, W, H);

  if (mapCanvas) {
    try {
      ctx.drawImage(mapCanvas, 0, 0, mapCanvas.width, mapCanvas.height, 0, 0, W, mapH);
    } catch (e) { /* tainted/lost context — keep the info panel */ }
  }

  // panel
  const py = mapH;
  ctx.fillStyle = theme.panel || theme.bg;
  ctx.fillRect(0, py, W, panelH);
  ctx.strokeStyle = theme.accent;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(0, py + 1); ctx.lineTo(W, py + 1); ctx.stroke();

  ctx.fillStyle = theme.accent;
  ctx.font = '600 26px Georgia, serif';
  ctx.fillText('PollGrid', 24, py + 38);
  ctx.fillStyle = theme.text;
  ctx.font = '20px Georgia, serif';
  ctx.fillText(opts.title || '', 140, py + 38);
  if (opts.subtitle) {
    ctx.fillStyle = theme.muted || theme.text;
    ctx.font = '13px sans-serif';
    ctx.fillText(opts.subtitle, 140, py + 60);
  }

  // legend swatches
  if (opts.legend && opts.legend.length) {
    let x = 24, y = py + 78;
    ctx.font = '12px sans-serif';
    for (const item of opts.legend) {
      ctx.fillStyle = item.color;
      ctx.fillRect(x, y - 10, 14, 14);
      ctx.strokeStyle = theme.line; ctx.lineWidth = 1;
      ctx.strokeRect(x, y - 10, 14, 14);
      ctx.fillStyle = theme.text;
      const label = item.label;
      ctx.fillText(label, x + 20, y + 1);
      x += 30 + ctx.measureText(label).width + 14;
      if (x > W - 200) { x = 24; y += 22; }
    }
  }

  // totals lines (builder toplines etc.)
  if (opts.totals) {
    ctx.font = '600 16px Menlo, monospace';
    opts.totals.forEach((line, i) => {
      ctx.fillStyle = theme.text;
      ctx.fillText(line, W - 24 - ctx.measureText(line).width, py + 40 + i * 26);
    });
  }
  return out;
}

export function downloadCanvas(canvas, filename = 'pollgrid.png') {
  const a = document.createElement('a');
  a.href = canvas.toDataURL('image/png');
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function downloadText(text, filename, mime = 'text/plain') {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime }));
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  a.remove();
}
