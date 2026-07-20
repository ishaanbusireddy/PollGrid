/* Tier3List.js — the accessible fallback renderer: a sortable, keyboard-
   navigable table of states carrying the same thematic values the canvas
   tiers shade with. Arrow keys move row focus, Enter opens the state,
   headers sort (click or Enter). */

export class Tier3List {
  constructor(container, statesGeo, hooks = {}) {
    this.container = container;
    this.hooks = hooks;
    this.states = statesGeo.features
      .map((f) => ({ key: f.id, name: f.properties.name }))
      .sort((a, b) => a.name.localeCompare(b.name));
    this.values = null;
    this.confidence = null;
    this.fmt = (v) => String(v);
    this.modeLabel = 'value';
    this.sortBy = 'name';
    this.sortDir = 1;

    this.root = document.createElement('div');
    this.root.className = 'tier3-wrap';
    container.appendChild(this.root);
    this.render();
  }

  setChoropleth({ values, confidence, fmt, label }) {
    this.values = values || null;
    this.confidence = confidence || null;
    if (fmt) this.fmt = fmt;
    if (label) this.modeLabel = label;
    this.render();
  }

  /* no-op renderer-interface methods (list view has no camera or overlays) */
  setCountiesGeo() {} setDistrictsGeo() {} setDistrictsVisible() {}
  setOverrideColors() {} setPins() {} refreshTheme() {} addThread() {}
  flyTo() {} flyToFeature() {} resize() {} getCanvas() { return null; }

  render() {
    const rows = [...this.states];
    rows.sort((a, b) => {
      if (this.sortBy === 'name') return a.name.localeCompare(b.name) * this.sortDir;
      const va = this.values?.[a.key], vb = this.values?.[b.key];
      if (va === undefined && vb === undefined) return 0;
      if (va === undefined) return 1;
      if (vb === undefined) return -1;
      return (va - vb) * this.sortDir;
    });

    this.root.innerHTML = '';
    const h = document.createElement('div');
    h.innerHTML = `<h2>State list view</h2>
      <p class="dim">Accessible renderer (Tier 3). Same thematic values as the map — sort with the headers, navigate rows with arrow keys, Enter opens a state.</p>`;
    this.root.appendChild(h);

    const table = document.createElement('table');
    table.className = 'grid';
    table.setAttribute('role', 'grid');
    const thead = document.createElement('thead');
    const tr = document.createElement('tr');
    for (const [col, label] of [['name', 'State'], ['value', this.modeLabel]]) {
      const th = document.createElement('th');
      th.tabIndex = 0;
      th.textContent = label + (this.sortBy === col ? (this.sortDir > 0 ? ' ▲' : ' ▼') : '');
      th.setAttribute('aria-sort', this.sortBy === col ? (this.sortDir > 0 ? 'ascending' : 'descending') : 'none');
      const sort = () => {
        if (this.sortBy === col) this.sortDir *= -1; else { this.sortBy = col === 'conf' ? 'value' : col; this.sortDir = 1; }
        this.render();
      };
      th.addEventListener('click', sort);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter') sort(); });
      tr.appendChild(th);
    }
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    for (const s of rows) {
      const trr = document.createElement('tr');
      trr.tabIndex = 0;
      trr.dataset.key = s.key;
      const v = this.values?.[s.key];
      trr.innerHTML = `<td>${s.name}</td>
        <td class="num">${v === undefined ? '<span class="dim">no data</span>' : this.fmt(v)}</td>`;
      const open = () => this.hooks.onPick && this.hooks.onPick({ tier: 'state', key: s.key, name: s.name });
      trr.addEventListener('click', open);
      trr.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') open();
        else if (e.key === 'ArrowDown') { e.preventDefault(); trr.nextElementSibling?.focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); trr.previousElementSibling?.focus(); }
      });
      tbody.appendChild(trr);
    }
    table.appendChild(tbody);
    this.root.appendChild(table);
  }

  destroy() { this.root.remove(); }
}
