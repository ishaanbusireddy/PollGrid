/* Analyst.js — the "Ask the Analyst" chat pane (#/analyst, or scoped to an
   entity from any page). POSTs /api/analyst/query, renders the answer with a
   streaming-look typewriter, citations as chips linking to the cited
   poll/fact/demographic row, and an honest "model:" footer — including
   "deterministic fallback" when that's what actually answered.
   Conversation history is kept per session_id (localStorage). */

import { escapeHtml } from './PollsWindow.js';

export class Analyst {
  /** @param {HTMLElement} el @param {{api:object, navigate:Function}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.entity = { type: 'nation', id: 'US', label: 'the national picture' };
    this._timer = null;
  }

  setEntity(type, id, label) {
    this.entity = { type, id, label: label || `${type} ${id}` };
  }

  _sessKey() { return `pollgrid.analyst.${this.entity.type}.${this.entity.id}`; }

  _loadSession() {
    try { return JSON.parse(localStorage.getItem(this._sessKey())) || { session_id: null, log: [] }; }
    catch (e) { return { session_id: null, log: [] }; }
  }

  _saveSession(s) {
    try { localStorage.setItem(this._sessKey(), JSON.stringify({ session_id: s.session_id, log: s.log.slice(-20) })); }
    catch (e) { /* private mode */ }
  }

  show() {
    this.session = this._loadSession();
    this.el.innerHTML = `
      <h1>The Analyst</h1>
      <p class="dim">Grounded Q&amp;A over <b>${escapeHtml(this.entity.label)}</b> — the model reasons over a pre-assembled, cited context pack and is never allowed to invent a number. Answers cite the specific poll, fact, or demographic row used.</p>
      <div class="analyst-log"></div>
      <form class="analyst-form">
        <input type="text" placeholder="Why is this race tightening? What does the demographic trend predict?" aria-label="Question for the Analyst">
        <button class="primary" type="submit">Ask</button>
      </form>`;
    this.log = this.el.querySelector('.analyst-log');
    for (const m of this.session.log) this._renderMsg(m, false);
    const form = this.el.querySelector('form');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const input = form.querySelector('input');
      const q = input.value.trim();
      if (q) { input.value = ''; this.ask(q); }
    });
  }

  async ask(question) {
    const qMsg = { role: 'q', text: question };
    this.session.log.push(qMsg);
    this._renderMsg(qMsg, false);

    const pending = document.createElement('div');
    pending.className = 'analyst-msg a';
    pending.innerHTML = '<span class="dim">thinking…</span><span class="caret">&nbsp;</span>';
    this.log.appendChild(pending);
    pending.scrollIntoView({ block: 'nearest' });

    let res = null, err = null;
    try {
      res = await this.bag.api.analystQuery({
        entity_type: this.entity.type,
        entity_id: this.entity.id,
        question,
        session_id: this.session.session_id || undefined,
      });
    } catch (e) { err = e; }
    pending.remove();

    if (!res) {
      const aMsg = {
        role: 'a',
        text: 'The Analyst is unreachable — the backend is offline or /api/analyst/query failed'
          + (err && err.status ? ` (HTTP ${err.status})` : '')
          + '. No answer was generated; nothing here is a guess.',
        model: 'unavailable', citations: [],
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
      const modelLabel = m.model === 'deterministic' ? 'deterministic fallback (no LLM was reachable)' : `model: ${m.model}`;
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
