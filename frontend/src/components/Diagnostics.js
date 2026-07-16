/* Diagnostics.js — (#/diagnostics, "Status" in the top nav) the operator's
   health page. Renders GET /api/diagnostics: the Analyst-LLM provider status
   (big and labeled, so "is my ollama working?" is answerable at a glance),
   the provenance hash-chain table, DB integrity checks, and the synthetic
   row count (with the purge_synthetic.py pointer) — plus the source-health
   table from GET /api/status. Everything degrades to informative empty
   states when routes are unavailable. */

import { escapeHtml } from './PollsWindow.js';

function healthChip(health) {
  const h = String(health || 'unknown').toLowerCase();
  const cls = h === 'ok' ? 'ok' : h === 'degraded' ? 'warn' : h === 'down' ? 'err' : '';
  return `<span class="chip ${cls}">${escapeHtml(h)}</span>`;
}

function truncate(s, n = 90) {
  const str = String(s ?? '');
  return str.length > n ? str.slice(0, n - 1) + '…' : str;
}

export class Diagnostics {
  /** @param {HTMLElement} el @param {{api:object}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
  }

  async show() {
    this.el.innerHTML = `
      <h1>Status &amp; diagnostics</h1>
      <p class="dim">Live platform health: the analyst LLM, provenance hash-chains, DB integrity, synthetic data, and every ingestion source.</p>
      <div class="diag-llm"><div class="dim">checking…</div></div>
      <div class="diag-foundation"></div>
      <div class="row mt" style="align-items:flex-start">
        <div class="panel diag-chains" style="flex:1;min-width:300px;margin:0"><div class="panel-head">Provenance hash-chains</div><div class="panel-body"><div class="dim">loading…</div></div></div>
        <div class="panel diag-integrity" style="flex:1;min-width:300px;margin:0"><div class="panel-head">Integrity checks</div><div class="panel-body"><div class="dim">loading…</div></div></div>
      </div>
      <div class="panel diag-synth"><div class="panel-head">Synthetic data</div><div class="panel-body"><div class="dim">loading…</div></div></div>
      <div class="panel diag-sources"><div class="panel-head">Source health (/api/status)</div><div class="panel-body"><div class="dim">loading…</div></div></div>`;

    const [diag, status] = await Promise.all([this.bag.api.diagnostics(), this.bag.api.status()]);
    if (!this.el.querySelector('.diag-llm')) return; // navigated away

    /* ----- Analyst LLM — the big "is my ollama working?" answer ----- */
    const llmBox = this.el.querySelector('.diag-llm');
    if (diag && diag.llm) {
      const provider = diag.llm.provider || 'none';
      const providerLabel = provider === 'ollama' ? 'Ollama' : provider;
      const reachable = !!diag.llm.reachable;
      llmBox.innerHTML = `
        <div class="diag-llm-card ${reachable ? 'up' : 'down'}">
          <div class="diag-llm-main">Analyst LLM: ${escapeHtml(providerLabel)} ${reachable ? 'reachable' : 'unreachable'}</div>
          <div class="diag-llm-sub">provider <b>${escapeHtml(provider)}</b> · model <b>${escapeHtml(diag.llm.model || '—')}</b>
            ${reachable ? '· prose &amp; rubric calls will use it' : '· platform falls back to deterministic prose — numbers never depended on it'}</div>
        </div>`;
    } else {
      llmBox.innerHTML = `<div class="empty">LLM status unavailable.<span class="why">GET /api/diagnostics failed — backend offline</span></div>`;
    }

    /* ----- data foundation — WHY maps are blank, answered at a glance ----- */
    const fBox = this.el.querySelector('.diag-foundation');
    const fd = status && status.data_foundation;
    if (fBox && fd) {
      const tiers = ['nation', 'state', 'county_equivalent', 'congressional_district'];
      const row = (name, obj) => `<tr><td class="mono" style="font-size:11px">${name}</td>
        ${tiers.map((t) => {
          const n = (obj && obj[t]) || 0;
          return `<td class="${n === 0 ? 'err' : ''}" style="${n === 0 ? 'color:var(--rep,#c04f4f);font-weight:700' : ''}">${n}</td>`;
        }).join('')}</tr>`;
      const banner = fd.healthy ? '' :
        `<div class="diag-llm-card down" style="margin:8px 0">
           <div class="diag-llm-main">Real data foundation is EMPTY — maps and scorecards will look blank</div>
           <div class="diag-llm-sub">The rows below are what every visible surface is computed from. Zeros mean the
           Census / OpenElections imports have not landed (check Source health below for the error, e.g. a missing
           CENSUS_API_KEY). Run <b>python scripts/bootstrap_real.py</b> and re-check.</div>
         </div>`;
      fBox.innerHTML = `${banner}
        <div class="panel" style="margin:8px 0"><div class="panel-head">Data foundation (real rows by tier)</div>
        <div class="panel-body"><table class="grid">
          <thead><tr><th>table</th>${tiers.map((t) => `<th>${t.replace('_', ' ')}</th>`).join('')}</tr></thead>
          <tbody>${row('political_history', fd.political_history)}${row('demographics', fd.demographics)}</tbody>
        </table></div></div>`;
    }

    /* ----- hash-chain table ----- */
    const chainsBox = this.el.querySelector('.diag-chains .panel-body');
    if (diag && diag.chains && diag.chains.length) {
      chainsBox.innerHTML = `<table class="grid"><thead><tr><th>Table</th><th>Chain</th><th>Detail</th></tr></thead>
        <tbody>${diag.chains.map((c) => `<tr>
          <td class="mono" style="font-size:11px">${escapeHtml(c.table)}</td>
          <td>${c.ok ? '<span class="chip ok">intact</span>' : '<span class="chip err">broken</span>'}</td>
          <td class="dim" style="font-size:11px">${escapeHtml(truncate(c.detail || '', 80))}</td>
        </tr>`).join('')}</tbody></table>`;
    } else {
      chainsBox.innerHTML = `<div class="empty">No chain report.<span class="why">GET /api/diagnostics failed or returned no chains</span></div>`;
    }

    /* ----- integrity checks ----- */
    const integBox = this.el.querySelector('.diag-integrity .panel-body');
    const integ = diag && diag.integrity;
    if (integ && Object.keys(integ).length) {
      integBox.innerHTML = Object.entries(integ).map(([check, count]) => `
        <div class="kv"><span class="k">${escapeHtml(check)}</span>
          <span class="v">${Number(count) === 0 ? '<span class="chip ok">0 violations</span>' : `<span class="chip err">${Number(count)} violation(s)</span>`}</span></div>`).join('');
    } else {
      integBox.innerHTML = `<div class="empty">No integrity report.<span class="why">GET /api/diagnostics failed or returned no checks</span></div>`;
    }

    /* ----- synthetic rows ----- */
    const synthBox = this.el.querySelector('.diag-synth .panel-body');
    if (diag && diag.synthetic_rows != null) {
      synthBox.innerHTML = `
        <div class="kv"><span class="k">rows flagged is_synthetic=1 (all tables)</span>
          <span class="v">${diag.synthetic_rows === 0 ? '<span class="chip ok">0</span>' : `<span class="chip warn">${Number(diag.synthetic_rows).toLocaleString()}</span>`}</span></div>
        <div class="dim" style="font-size:11px;margin-top:6px">Synthetic demo rows are always flagged and badged <span class="chip warn">SYNTH</span> in the UI.
          Remove them all with <span class="mono">python scripts/purge_synthetic.py</span>.</div>
        ${diag.db_path ? `<div class="dim mono" style="font-size:10px;margin-top:6px">db: ${escapeHtml(diag.db_path)}</div>` : ''}`;
    } else {
      synthBox.innerHTML = `<div class="empty">Synthetic-row count unavailable.<span class="why">GET /api/diagnostics failed</span></div>`;
    }

    /* ----- source health ----- */
    const srcBox = this.el.querySelector('.diag-sources .panel-body');
    if (status && status.sources && status.sources.length) {
      srcBox.innerHTML = `<table class="grid"><thead><tr><th>Source</th><th>Health</th><th>Last run</th><th>Last error</th></tr></thead>
        <tbody>${status.sources.map((s) => `<tr>
          <td>${escapeHtml(s.name || s.id)} ${s.is_active === 0 || s.is_active === false ? '<span class="chip">inactive</span>' : ''}</td>
          <td>${healthChip(s.health)}</td>
          <td class="mono" style="font-size:11px">${escapeHtml(String(s.last_run_at || '—').slice(0, 19))}</td>
          <td class="dim" style="font-size:11px" title="${escapeHtml(String(s.last_error || ''))}">${escapeHtml(truncate(s.last_error || '—'))}</td>
        </tr>`).join('')}</tbody></table>`;
    } else {
      srcBox.innerHTML = `<div class="empty">Source health unavailable.<span class="why">GET /api/status failed — backend offline</span></div>`;
    }
  }
}
