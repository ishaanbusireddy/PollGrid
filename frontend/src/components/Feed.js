/* Feed.js — the right-rail live feed of correlated story clusters (never raw
   articles). Cards arrive over the WebSocket (or the REST fallback poll);
   clicking a card flies the map to the story's geography and opens the race
   pane. New cards get a CSS entrance animation. */

const CATEGORY_CHIPS = {
  polling: 'accent', finance: 'other', legislation: 'other', endorsement: 'ok',
  scandal: 'err', debate: 'warn', 'election-result': 'accent', rhetoric: 'warn',
  'campaign-event': 'other',
};

export class Feed {
  /** @param {HTMLElement} el @param {{navigate:Function, flyToState:Function}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.seen = new Set();
    this.el.innerHTML = `
      <div class="feed-head">
        <h3>Live feed</h3>
        <span class="feed-status" title="Feed transport status">connecting…</span>
      </div>
      <div class="feed-body"></div>`;
    this.body = this.el.querySelector('.feed-body');
    this.statusEl = this.el.querySelector('.feed-status');
    this._empty();
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

  /** Add a story card. story: {id, headline, category, race_id, state_fips, score} */
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
        ${story.score != null ? `<span class="story-score">score ${Number(story.score).toFixed(2)}</span>` : ''}
      </div>`;
    card.addEventListener('click', () => {
      if (story.state_fips) this.bag.flyToState(String(story.state_fips).padStart(2, '0'));
      if (story.race_id) this.bag.navigate(`#/race/${story.race_id}`);
      else if (story.state_fips) this.bag.navigate(`#/state/${String(story.state_fips).padStart(2, '0')}`);
    });
    this.body.prepend(card);
    while (this.body.children.length > 60) this.body.lastElementChild.remove();
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
