/* CommandPalette.js — Ctrl/Cmd+K fuzzy jump to any race, candidate, or state.
   States are matched client-side from the vendored boundary file; races and
   candidates are queried live (/api/races, /api/candidates?query=) and
   degrade silently to states-only when the backend is down. */

function fuzzyScore(query, target) {
  // subsequence match, rewarding starts-with and word boundaries
  const q = query.toLowerCase(), t = target.toLowerCase();
  if (!q) return 1;
  if (t.startsWith(q)) return 100 - t.length * 0.1;
  let qi = 0, score = 0, streak = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      qi++;
      streak++;
      score += 2 + streak + (ti === 0 || t[ti - 1] === ' ' ? 4 : 0);
    } else streak = 0;
  }
  return qi === q.length ? score - t.length * 0.05 : -1;
}

export class CommandPalette {
  /** @param {HTMLElement} root @param {{navigate:Function, api:object, states:Array<{key,name}>}} bag */
  constructor(root, bag) {
    this.root = root;
    this.bag = bag;
    this.open = false;
    this.items = [];
    this.sel = 0;
    this._raceCache = null;
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); this.toggle(); }
      else if (e.key === 'Escape' && this.open) this.hide();
    });
  }

  toggle() { this.open ? this.hide() : this.show(); }

  show() {
    this.open = true;
    this.root.innerHTML = `
      <div class="palette-overlay">
        <div class="palette-box" role="dialog" aria-label="Command palette">
          <input type="text" placeholder="Jump to a race, candidate, or state…" aria-label="Search">
          <div class="palette-list"></div>
        </div>
      </div>`;
    this.overlay = this.root.querySelector('.palette-overlay');
    this.input = this.root.querySelector('input');
    this.list = this.root.querySelector('.palette-list');
    this.overlay.addEventListener('pointerdown', (e) => { if (e.target === this.overlay) this.hide(); });
    this.input.addEventListener('input', () => this._query(this.input.value));
    this.input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); this._move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); this._move(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); this._go(); }
    });
    this.input.focus();
    this._query('');
  }

  hide() {
    this.open = false;
    this.root.innerHTML = '';
  }

  async _query(q) {
    const results = [];
    for (const s of this.bag.states) {
      const sc = fuzzyScore(q, s.name);
      if (sc >= 0) results.push({ kind: 'state', label: s.name, score: sc, hash: `#/state/${s.key}` });
    }
    const token = (this._token = Symbol());
    // remote sources, silently absent when the backend is down
    if (q.length >= 1) {
      const [races, cands] = await Promise.all([
        this._races(),
        q.length >= 2 ? this.bag.api.candidates({ query: q, limit: 12 }) : null,
      ]);
      if (this._token !== token || !this.open) return;
      for (const r of races || []) {
        const sc = fuzzyScore(q, r.name || '');
        if (sc >= 0) results.push({ kind: 'race', label: r.name, score: sc + 5, hash: `#/race/${r.id}` });
      }
      for (const c of cands || []) {
        results.push({ kind: 'candidate', label: `${c.name} (${c.party_code || '—'})`, score: fuzzyScore(q, c.name) + 5, hash: `#/candidate/${c.id}` });
      }
    }
    results.sort((a, b) => b.score - a.score);
    this.items = results.slice(0, 14);
    this.sel = 0;
    this._render();
  }

  async _races() {
    if (this._raceCache === null) this._raceCache = (await this.bag.api.races({})) || [];
    return this._raceCache;
  }

  _render() {
    this.list.innerHTML = '';
    if (!this.items.length) {
      this.list.innerHTML = '<div class="palette-item dim">No matches</div>';
      return;
    }
    this.items.forEach((it, i) => {
      const el = document.createElement('div');
      el.className = 'palette-item' + (i === this.sel ? ' sel' : '');
      el.innerHTML = `<span class="pi-kind">${it.kind}</span><span>${it.label}</span>`;
      el.addEventListener('click', () => { this.sel = i; this._go(); });
      this.list.appendChild(el);
    });
  }

  _move(d) {
    if (!this.items.length) return;
    this.sel = (this.sel + d + this.items.length) % this.items.length;
    this._render();
    this.list.children[this.sel]?.scrollIntoView({ block: 'nearest' });
  }

  _go() {
    const it = this.items[this.sel];
    if (!it) return;
    this.hide();
    this.bag.navigate(it.hash);
  }
}
