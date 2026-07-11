/* Feed.js — the right-rail live feed of correlated story clusters (never raw
   articles). Cards arrive over the WebSocket (or the REST fallback poll);
   clicking a card flies the map to the story's geography and opens the
   story-detail page (#/story/{id}). New cards get a CSS entrance animation.
   The rail also hosts, above the cards: the daily briefing card
   (GET /api/briefings/latest, hidden on 404) and the watchlist strip
   (GET /api/watchlist, hidden when the route is unavailable). */

const CATEGORY_CHIPS = {
  polling: 'accent', finance: 'other', legislation: 'other', endorsement: 'ok',
  scandal: 'err', debate: 'warn', 'election-result': 'accent', rhetoric: 'warn',
  'campaign-event': 'other',
};

const WATCH_HASH = {
  race: (id) => `#/race/${id}`,
  state: (id) => `#/state/${id}`,
  candidate: (id) => `#/candidate/${id}`,
};

export class Feed {
  /** @param {HTMLElement} el @param {{api:object, navigate:Function, flyToState:Function}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.seen = new Set();
    this.el.innerHTML = `
      <div class="feed-head">
        <h3>Live feed</h3>
        <span class="feed-status" title="Feed transport status">connecting…</span>
      </div>
      <div class="watch-strip" hidden></div>
      <div class="briefing-card" hidden></div>
      <div class="feed-body"></div>`;
    this.body = this.el.querySelector('.feed-body');
    this.statusEl = this.el.querySelector('.feed-status');
    this.watchEl = this.el.querySelector('.watch-strip');
    this.briefEl = this.el.querySelector('.briefing-card');
    this._empty();
    this.loadBriefing();
    this.refreshWatchlist();
  }

  _empty() {
    this.emptyEl = document.createElement('div');
    this.emptyEl.className = 'empty';
    this.emptyEl.innerHTML = 'No story clusters yet.<span class="why">stories arrive over /ws/feed (REST fallback: /api/stories)</span>';
    this.body.appendChild(this.emptyEl);
  }

  setTransport(label, live) {
    this.statusEl.textContent = label;
    this.statusEl.classList.toggle('live', !!live);
  }

  /* ----- daily briefing card (404 -> hidden) ----- */

  async loadBriefing() {
    const b = await this.bag.api.briefingLatest();
    if (!b || !b.body) { this.briefEl.hidden = true; return; }
    const modelLabel = !b.model || b.model === 'deterministic'
      ? 'deterministic briefing' : `model: ${b.model}`;
    this.briefEl.innerHTML = `
      <div class="briefing-head">Daily briefing <span class="mono dim">${escapeHtml(String(b.as_of || ''))}</span></div>
      <div class="briefing-body">${escapeHtml(b.body)}</div>
      <div class="briefing-model" title="honest generation label">${escapeHtml(modelLabel)}</div>`;
    this.briefEl.hidden = false;
  }

  /* ----- watchlist strip (route unavailable -> hidden) ----- */

  async refreshWatchlist() {
    const rows = await this.bag.api.watchlist();
    if (rows === null) { this.watchEl.hidden = true; return; }
    if (!rows.length) {
      this.watchEl.innerHTML = `<span class="watch-title">★ Watchlist</span><span class="dim" style="font-size:10px">empty — star a race, state, or candidate</span>`;
      this.watchEl.hidden = false;
      return;
    }
    this.watchEl.innerHTML = `<span class="watch-title">★ Watchlist</span>`;
    for (const w of rows) {
      const chip = document.createElement('span');
      chip.className = 'chip accent watch-chip';
      chip.textContent = w.label || `${w.entity_type} ${w.entity_id}`;
      chip.title = `${w.entity_type} — open`;
      const hashFn = WATCH_HASH[w.entity_type];
      if (hashFn) chip.addEventListener('click', () => this.bag.navigate(hashFn(w.entity_id)));
      this.watchEl.appendChild(chip);
    }
    this.watchEl.hidden = false;
  }

  /** Add a story card. story: {id, headline, category, race_id, state_fips, score, is_synthetic} */
  addStory(story, animate = true) {
    if (!story || (story.id != null && this.seen.has(story.id))) return;
    if (story.id != null) this.seen.add(story.id);
    if (this.emptyEl) { this.emptyEl.remove(); this.emptyEl = null; }

    const card = document.createElement('div');
    card.className = 'story-card' + (animate ? ' enter' : '');
    const chip = CATEGORY_CHIPS[story.category] || '';
    card.innerHTML = `
      <div class="story-head">${escapeHtml(story.headline || '(untitled story)')}</div>
      <div class="story-meta">
        ${story.category ? `<span class="chip ${chip}">${escapeHtml(story.category)}</span>` : ''}
        ${story.race_id ? `<span class="chip accent">race #${story.race_id}</span>` : ''}
        ${story.is_synthetic ? '<span class="chip warn synth-chip" title="synthetic demo row — remove with scripts/purge_synthetic.py">SYNTH</span>' : ''}
        ${story.score != null ? `<span class="story-score">score ${Number(story.score).toFixed(2)}</span>` : ''}
      </div>`;
    card.addEventListener('click', () => {
      if (story.state_fips) this.bag.flyToState(String(story.state_fips).padStart(2, '0'));
      if (story.id != null) this.bag.navigate(`#/story/${story.id}`);
      else if (story.race_id) this.bag.navigate(`#/race/${story.race_id}`);
      else if (story.state_fips) this.bag.navigate(`#/state/${String(story.state_fips).padStart(2, '0')}`);
    });
    this.body.prepend(card);
    while (this.body.children.length > 60) this.body.lastElementChild.remove();
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
