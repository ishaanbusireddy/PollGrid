/* Analyst.js — the "Ask the Analyst" assistant, now a floating accent ORB
   (bottom-right, on every view) that toggles a RIGHT-DOCKED chat panel. The
   panel POSTs /api/analyst/query and renders the answer with a streaming-look
   typewriter, citations as chips linking to the cited poll/fact/demographic
   row, and an honest "model:" footer — including "deterministic fallback" when
   that's what actually answered.

   The panel AUTO-READS the current on-screen context: App wires SlidePane opens
   and map selection through setEntity(), so the analyst's entity_type+entity_id
   track whatever race / state / county / district is open (nation 'US'
   otherwise). Conversation history is kept per entity session (localStorage). */

import { escapeHtml } from './PollsWindow.js';

export class Analyst {
  /** @param {{api:object, navigate:Function}} bag */
  constructor(bag) {
    this.bag = bag;
    this.entity = { type: 'nation', id: 'US', label: 'the national picture' };
    this.open = false;
    this._timer = null;
    this._buildDom();
  }

  /* ----- floating orb + docked panel (built once, live for the whole session) ----- */

  _buildDom() {
    this.orb = document.createElement('button');
    this.orb.className = 'analyst-orb';
    this.orb.type = 'button';
    this.orb.title = 'Ask the Analyst';
    this.orb.setAttribute('aria-label', 'Ask the Analyst');
    this.orb.innerHTML = '<span class="orb-glyph" aria-hidden="true">✦</span>';
    this.orb.addEventListener('click', () => this.toggle());
    document.body.appendChild(this.orb);

    this.panel = document.createElement('aside');
    this.panel.className = 'analyst-dock';
    this.panel.setAttribute('aria-label', 'The Analyst');
    this.panel.innerHTML = `
      <div class="analyst-dock-head">
        <span class="ad-title">The Analyst</span>
        <button class="ad-close" type="button" title="Close">✕</button>
      </div>
      <div class="analyst-context"></div>
      <div class="analyst-log"></div>
      <form class="analyst-form">
        <input type="text" placeholder="Why is this race tightening?" aria-label="Question for the Analyst">
        <button class="primary" type="submit">Ask</button>
      </form>
      <div class="analyst-foot dim">Grounded Q&amp;A over a pre-assembled, cited context pack — the model is never allowed to invent a number.</div>`;
    document.body.appendChild(this.panel);

    this.ctxEl = this.panel.querySelector('.analyst-context');
    this.log = this.panel.querySelector('.analyst-log');
    this.panel.querySelector('.ad-close').addEventListener('click', () => this.closePanel());
    const form = this.panel.querySelector('form');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const input = form.querySelector('input');
      const q = input.value.trim();
      if (q) { input.value = ''; this.ask(q); }
    });
    this._renderContext();
  }

  /* ----- context (auto-read from the current selection) ----- */

  /** Point the analyst at an entity. Same type+id just refines the label (the
      chat is kept); a genuinely new entity swaps the per-entity session. */
  setEntity(type, id, label) {
    const changed = type !== this.entity.type || String(id) !== String(this.entity.id);
    this.entity = { type, id, label: label || `${type} ${id}` };
    this._renderContext();
    if (changed && this.open) this._reloadSession();
  }

  _renderContext() {
    if (this.ctxEl) this.ctxEl.innerHTML = `context: <b>${escapeHtml(this.entity.label)}</b>`;
  }

  /* ----- open / close / toggle ----- */

  openPanel() {
    this.open = true;
    this.panel.classList.add('open');
    this.orb.classList.add('active');
    this._reloadSession();
    this._renderContext();
    const input = this.panel.querySelector('input');
    if (input) setTimeout(() => input.focus(), 60);
  }

  closePanel() {
    this.open = false;
    this.panel.classList.remove('open');
    this.orb.classList.remove('active');
    clearInterval(this._timer);
  }

  toggle() { this.open ? this.closePanel() : this.openPanel(); }

  /** #/analyst alias — just opens the docked panel over the current view. */
  show() { this.openPanel(); }

  /* ----- per-entity session (localStorage) ----- */

  _sessKey() { return `pollgrid.analyst.${this.entity.type}.${this.entity.id}`; }

  _loadSession() {
    try { return JSON.parse(localStorage.getItem(this._sessKey())) || { session_id: null, log: [] }; }
    catch (e) { return { session_id: null, log: [] }; }
  }

  _saveSession(s) {
    try { localStorage.setItem(this._sessKey(), JSON.stringify({ session_id: s.session_id, log: s.log.slice(-20) })); }
    catch (e) { /* private mode */ }
  }

  _reloadSession() {
    clearInterval(this._timer);
    this.session = this._loadSession();
    this.log.innerHTML = '';
    for (const m of this.session.log) this._renderMsg(m, false);
  }

  /* ----- ask ----- */

  async ask(question) {
    if (!this.session) this.session = this._loadSession();
    const qMsg = { role: 'q', text: question };
    this.session.log.push(qMsg);
    this._renderMsg(qMsg, false);

    // thinking bubble with a live elapsed timer + cancel button — a long local-LLM
    // generation must never read as a silent hang the user can't escape
    const pending = document.createElement('div');
    pending.className = 'analyst-msg a';
    pending.innerHTML = '<span class="dim">thinking… <span class="elapsed">0s</span></span> '
      + '<button class="chip warn" style="cursor:pointer">cancel</button>';
    this.log.appendChild(pending);
    pending.scrollIntoView({ block: 'nearest' });
    const controller = new AbortController();
    const t0 = performance.now();
    const timer = setInterval(() => {
      const el = pending.querySelector('.elapsed');
      if (el) el.textContent = `${Math.round((performance.now() - t0) / 1000)}s`;
    }, 1000);
    pending.querySelector('button').onclick = () => controller.abort();

    let res = null, err = null;
    try {
      res = await this.bag.api.analystQuery({
        entity_type: this.entity.type,
        entity_id: this.entity.id,
        question,
        session_id: this.session.session_id || undefined,
      }, { signal: controller.signal });
    } catch (e) { err = e; }
    clearInterval(timer);
    pending.remove();

    if (!res) {
      const cancelled = err && err.message === 'cancelled';
      const aMsg = {
        role: 'a',
        text: cancelled
          ? 'Cancelled. (The local model may take a while on a full question — the answer was abandoned, nothing was guessed.)'
          : 'The Analyst is unreachable — the backend is offline or /api/analyst/query failed'
            + (err && err.status ? ` (HTTP ${err.status})` : '')
            + '. No answer was generated; nothing here is a guess.',
        model: cancelled ? 'cancelled' : 'unavailable', citations: [],
      };
      this.session.log.push(aMsg);
      this._saveSession(this.session);
      this._renderMsg(aMsg, true);
      return;
    }
    this.session.session_id = res.session_id || this.session.session_id;
    const aMsg = {
      role: 'a', text: res.answer || '(empty answer)',
      model: res.model || '?', citations: res.citations || [], stale: !!res.pack_stale,
    };
    this.session.log.push(aMsg);
    this._saveSession(this.session);
    this._renderMsg(aMsg, true);
  }

  _renderMsg(m, typewriter) {
    const el = document.createElement('div');
    el.className = 'analyst-msg ' + (m.role === 'q' ? 'q' : 'a');
    if (m.role === 'q') {
      el.textContent = m.text;
      this.log.appendChild(el);
      return;
    }
    const body = document.createElement('span');
    el.appendChild(body);
    this.log.appendChild(el);

    const finish = () => {
      body.textContent = m.text;
      const cites = document.createElement('div');
      for (const c of m.citations || []) {
        const chip = document.createElement('span');
        chip.className = 'cite-chip';
        chip.textContent = `${c.kind || 'ref'}: ${c.label || c.ref}`;
        chip.title = 'open cited source';
        chip.addEventListener('click', () => this._openCitation(c));
        cites.appendChild(chip);
      }
      el.appendChild(cites);
      const foot = document.createElement('span');
      foot.className = 'model-note';
      // the backend already states the honest reason for a deterministic answer
      // (no provider / timed out / thin data) inside the answer text itself — don't
      // hard-code "no LLM was reachable" here, which is often false
      const modelLabel = m.model === 'deterministic' ? 'deterministic answer (reason in text above)' : `model: ${m.model}`;
      foot.textContent = modelLabel + (m.stale ? ' · context pack stale' : '');
      el.appendChild(foot);
      el.scrollIntoView({ block: 'nearest' });
    };

    if (!typewriter) { finish(); return; }
    // streaming-look typewriter over the already-complete answer text
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.innerHTML = '&nbsp;';
    el.appendChild(caret);
    let i = 0;
    clearInterval(this._timer);
    this._timer = setInterval(() => {
      i = Math.min(m.text.length, i + 3);
      body.textContent = m.text.slice(0, i);
      if (i >= m.text.length) {
        clearInterval(this._timer);
        caret.remove();
        el.remove();
        this._renderMsg(m, false);
      }
    }, 16);
  }

  _openCitation(c) {
    const ref = String(c.ref ?? '');
    if (c.kind === 'poll') this.bag.navigate('#/polls');
    else if (c.kind === 'race') this.bag.navigate(`#/race/${ref}`);
    else if (c.kind === 'candidate') this.bag.navigate(`#/candidate/${ref}`);
    else if (c.kind === 'demographic' && /^\d{5}$/.test(ref)) this.bag.navigate(`#/county/${ref}`);
    else if (c.kind === 'demographic' && /^\d{2}$/.test(ref)) this.bag.navigate(`#/state/${ref}`);
    // other kinds (fact ids…) have no page of their own; the chip is the label
  }
}
