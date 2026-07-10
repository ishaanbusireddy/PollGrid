/* TimeScrubber.js — the bottom time-capsule scrubber. Sets the global asOf
   (snapshot semantics: every api.js read then carries ?as_of=). A "LIVE"
   snap returns to the present; when not live a persistent banner shows the
   viewing date. The slider spans from the earliest-data date to today —
   without a backend the range defaults to a two-year window, honestly
   labeled (deeper history is the archive's job, not the scrubber's). */

const DAY = 86400000;

export class TimeScrubber {
  /** @param {HTMLElement} el @param {{onAsOf:(dateStr|null)=>void}} bag */
  constructor(el, bag) {
    this.el = el;
    this.bag = bag;
    this.today = new Date();
    this.earliest = new Date(this.today.getTime() - 730 * DAY); // default window
    this.asOf = null; // null = LIVE
    this.render();
  }

  /** Give the scrubber a real earliest-data date when one is known. */
  setEarliest(dateStr) {
    const d = new Date(dateStr);
    if (!isNaN(d) && d < this.today) { this.earliest = d; this.render(); }
  }

  _days() { return Math.max(1, Math.round((this.today - this.earliest) / DAY)); }

  render() {
    const days = this._days();
    const cur = this.asOf ? Math.round((new Date(this.asOf) - this.earliest) / DAY) : days;
    this.el.innerHTML = '';

    const label = document.createElement('span');
    label.className = 'scrub-date mono dim';
    label.textContent = this.earliest.toISOString().slice(0, 10);

    const range = document.createElement('input');
    range.type = 'range';
    range.min = '0'; range.max = String(days); range.value = String(cur);
    range.setAttribute('aria-label', 'Time capsule: view data as of date');
    range.addEventListener('input', () => {
      const v = +range.value;
      if (v >= days) this._set(null);
      else this._set(new Date(this.earliest.getTime() + v * DAY).toISOString().slice(0, 10));
      liveBtn.classList.toggle('live', !this.asOf);
      liveBtn.textContent = this.asOf ? 'GO LIVE' : 'LIVE';
      curLabel.textContent = this.asOf || 'now';
    });

    const curLabel = document.createElement('span');
    curLabel.className = 'scrub-date mono';
    curLabel.textContent = this.asOf || 'now';

    const liveBtn = document.createElement('button');
    liveBtn.className = 'live-btn' + (this.asOf ? '' : ' live');
    liveBtn.textContent = this.asOf ? 'GO LIVE' : 'LIVE';
    liveBtn.addEventListener('click', () => {
      range.value = String(this._days());
      this._set(null);
      this.render();
    });

    this.el.append(label, range, curLabel, liveBtn);
  }

  _set(asOf) {
    if (asOf === this.asOf) return;
    this.asOf = asOf;
    this.bag.onAsOf(asOf);
  }
}
