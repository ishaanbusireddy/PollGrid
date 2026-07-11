/* Settings.js — (#/settings, "Settings" in the top nav) the managed-API-key
   console (mirrors GlobeGrid's). Reads GET /api/settings/keys: a prominent
   Analyst-LLM status block at the top ("is my Ollama up?"), then one card per
   managed key — masked value + configured badge when set, a password input and
   a "Save & test" button that POSTs and reports the *live* validation result
   (green ✓ / red ✗ with the provider's own detail), plus the free-signup link.
   Keys live in a repo-root .env, never in the database — stated on the page.
   Everything degrades to an informative empty state when the route is down. */

import { escapeHtml } from './PollsWindow.js';

/** The signup strings carry a trailing note ("… — free, instant."); split the
    clean URL out for the href and keep the rest as a caption. */
function splitSignup(signup) {
  const s = String(signup || '');
  const m = s.match(/https?:\/\/\S+/);
  if (!m) return { url: null, note: s };
  const url = m[0].replace(/[.,)]+$/, '');
  const note = s.replace(m[0], '').replace(/^[\s—–-]+/, '').trim();
  return { url, note };
}

export class Settings {
  /** @param {HTMLElement} el @param {{api:object}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
  }

  async show() {
    this.el.innerHTML = `
      <h1>Settings</h1>
      <p class="dim">Managed API keys for the ingestion sources and the Analyst LLM. Keys are stored in a local <span class="mono">.env</span> file and applied live — never written to the database, so an exported <span class="mono">pollgrid.db</span> never carries a secret. Every key is optional; each source and every AI feature has a deterministic fallback.</p>
      <div class="settings-llm"><div class="dim">checking the Analyst LLM…</div></div>
      <div class="settings-keys"><div class="dim">loading keys…</div></div>
      <div class="settings-env dim"></div>`;

    const data = await this.bag.api.settingsKeys();
    if (!this.el.querySelector('.settings-llm')) return; // navigated away

    if (!data) {
      this.el.querySelector('.settings-llm').innerHTML =
        `<div class="empty">Settings unavailable.<span class="why">GET /api/settings/keys failed — backend offline</span></div>`;
      this.el.querySelector('.settings-keys').innerHTML = '';
      return;
    }

    this._renderLlm(data.llm || {});
    this._renderKeys(data.keys || []);
    const env = this.el.querySelector('.settings-env');
    if (env) {
      env.innerHTML = data.env_path
        ? `Keys are written to <span class="mono">${escapeHtml(data.env_path)}</span> — a local .env file, never the database.`
        : 'Keys are written to a local .env file, never the database.';
    }
  }

  _renderLlm(llm) {
    const box = this.el.querySelector('.settings-llm');
    const provider = llm.provider || null;
    const reachable = !!llm.reachable;
    const isOllama = provider && String(provider).toLowerCase().includes('ollama');
    let cls, main, sub;
    if (reachable && isOllama) {
      cls = 'up';
      main = `Local Ollama: running${llm.model ? ` (${escapeHtml(String(llm.model))})` : ''}`;
      sub = 'The Analyst, narratives, and rubric scores use your local model — nothing leaves this machine, and no stored number ever depends on it.';
    } else if (reachable && provider) {
      cls = 'up';
      main = `Cloud AI: ${escapeHtml(String(provider))}${llm.model ? ` · ${escapeHtml(String(llm.model))}` : ''} — reachable`;
      sub = 'A cloud provider is answering Analyst prose. Every AI feature still has a deterministic fallback.';
    } else {
      cls = 'amber';
      main = 'No AI provider — Analyst uses deterministic fallback';
      sub = 'Start Ollama locally, or add a Claude / Groq / OpenRouter key below. The platform stays fully functional either way; only the prose changes, never a stored number.';
    }
    box.innerHTML = `
      <div class="diag-llm-card ${cls}">
        <div class="diag-llm-main">${main}</div>
        <div class="diag-llm-sub">${sub}</div>
      </div>`;
  }

  _renderKeys(keys) {
    const box = this.el.querySelector('.settings-keys');
    box.innerHTML = '';
    if (!keys.length) {
      box.innerHTML = `<div class="empty">No managed keys reported.<span class="why">GET /api/settings/keys returned an empty key list</span></div>`;
      return;
    }
    for (const k of keys) box.appendChild(this._keyCard(k));
  }

  _keyCard(k) {
    const card = document.createElement('div');
    card.className = 'key-card';
    const { url, note } = splitSignup(k.signup);
    card.innerHTML = `
      <div class="key-card-head">
        <span class="key-label">${escapeHtml(k.label || k.name)}</span>
        ${k.required ? '<span class="chip warn">required</span>' : '<span class="chip">optional</span>'}
        <span class="key-badge">${k.configured ? '<span class="chip ok">configured ✓</span>' : ''}</span>
      </div>
      <div class="key-enables dim">${escapeHtml(k.enables || '')}</div>
      <div class="key-current">${k.configured && k.masked ? `<span class="chip accent key-masked" title="stored value (masked)">${escapeHtml(k.masked)}</span>` : '<span class="dim" style="font-size:11px">not set</span>'}</div>
      <div class="key-row">
        <input type="password" autocomplete="off" spellcheck="false" placeholder="paste a new key…" aria-label="${escapeHtml(k.label || k.name)}">
        <button class="primary key-save" type="button">Save &amp; test</button>
      </div>
      <div class="key-result" hidden></div>
      <div class="key-signup dim">
        ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">get a free key →</a>` : ''}
        ${note ? `<span class="key-signup-note">${escapeHtml(note)}</span>` : ''}
      </div>`;

    const input = card.querySelector('input');
    const btn = card.querySelector('.key-save');
    const result = card.querySelector('.key-result');
    const badge = card.querySelector('.key-badge');
    const current = card.querySelector('.key-current');

    const showResult = (ok, detail) => {
      result.hidden = false;
      result.className = 'key-result ' + (ok ? 'ok' : 'err');
      result.textContent = (ok ? '✓ ' : '✗ ') + (detail || (ok ? 'saved' : 'rejected'));
    };

    const save = async () => {
      const value = input.value.trim();
      if (!value) { showResult(false, 'enter a key first'); return; }
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = 'testing…';
      result.hidden = false;
      result.className = 'key-result dim';
      result.textContent = 'validating against the provider…';
      const res = await this.bag.api.settingsSaveKey(k.name, value);
      btn.disabled = false;
      btn.textContent = prev;
      showResult(!!res.ok, res.detail);
      if (res.ok) {
        input.value = '';
        badge.innerHTML = '<span class="chip ok">configured ✓</span>';
        // reflect a fresh masked preview (server returns configured=true; mask locally)
        current.innerHTML = `<span class="chip accent key-masked" title="stored value (masked)">${escapeHtml(maskLocal(value))}</span>`;
      }
    };

    btn.addEventListener('click', save);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
    return card;
  }
}

/** Mirror core.keys._mask so the card updates without a full refetch. */
function maskLocal(value) {
  return value.length > 10 ? value.slice(0, 5) + '…' + value.slice(-3) : 'set';
}
