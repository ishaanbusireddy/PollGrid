/* api.js — thin fetch wrapper over the frozen PollGrid API contract.
   Every read passes through the global asOf (time-capsule) when set.
   Nothing here invents routes; anything that 404s / fails resolves to null
   via try*() so callers render an informative empty state instead of crashing. */

export class ApiError extends Error {
  constructor(status, message) { super(message || `HTTP ${status}`); this.status = status; }
}

export class Api {
  /** @param {() => string|null} getAsOf returns 'YYYY-MM-DD' or null for LIVE */
  constructor(getAsOf) {
    this.getAsOf = getAsOf || (() => null);
    this.backendUp = null; // null = unknown, set by status()
  }

  _url(path, params = {}) {
    const u = new URL(path, location.origin);
    const asOf = this.getAsOf();
    if (asOf) u.searchParams.set('as_of', asOf);
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, String(v));
    }
    return u;
  }

  async get(path, params) {
    let res;
    try {
      res = await fetch(this._url(path, params));
    } catch (e) {
      throw new ApiError(0, 'network unreachable');
    }
    if (!res.ok) throw new ApiError(res.status);
    return res.json();
  }

  /** Resolves null instead of throwing — the standard call shape for the UI. */
  async tryGet(path, params) {
    try { return await this.get(path, params); } catch (e) { return null; }
  }

  async post(path, body, { signal } = {}) {
    let res;
    try {
      res = await fetch(this._url(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal,
      });
    } catch (e) {
      if (e && e.name === 'AbortError') throw new ApiError(0, 'cancelled');
      throw new ApiError(0, 'network unreachable');
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch (_) { /* keep default */ }
      throw new ApiError(res.status, msg);
    }
    return res.status === 204 ? null : res.json();
  }

  /* ----- status & meta ----- */
  async status() {
    const s = await this.tryGet('/api/status');
    this.backendUp = !!s;
    return s;
  }
  config()       { return this.tryGet('/api/config'); }
  diagnostics()  { return this.tryGet('/api/diagnostics'); }

  /* ----- geography & demographics ----- */
  geoStates()          { return this.tryGet('/api/geo/states'); }
  geoCounties(state)   { return this.tryGet('/api/geo/counties', { state }); }
  geoDistricts(state)  { return this.tryGet('/api/geo/districts', { state }); }
  demographics(tier, entityId) { return this.tryGet(`/api/demographics/${tier}/${entityId}`); }
  history(tier, id)    { return this.tryGet(`/api/entities/${tier}/${id}/history`); }

  /* ----- races ----- */
  races(filters = {}) { return this.tryGet('/api/races', filters); }
  race(id)            { return this.tryGet(`/api/races/${id}`); }
  framing(id)         { return this.tryGet(`/api/races/${id}/framing`); }
  factors(raceId)     { return this.tryGet(`/api/factors/${raceId}`); }
  ensemble(raceId)    { return this.tryGet(`/api/forecast/ensemble/${raceId}`); }

  /* ----- polls ----- */
  polls(filters = {}) { return this.tryGet('/api/polls', filters); }

  /* ----- candidates & parties ----- */
  candidates(filters = {}) { return this.tryGet('/api/candidates', filters); }
  candidate(id)  { return this.tryGet(`/api/candidates/${id}`); }
  parties()      { return this.tryGet('/api/parties'); }
  party(id)      { return this.tryGet(`/api/parties/${id}`); }

  /* ----- articles ----- */
  articles(entityType, id, sort = 'recency', limit = 50) {
    return this.tryGet(`/api/articles/${entityType}/${id}`, { sort, limit });
  }

  /* ----- intelligence ----- */
  chamber(chamber)     { return this.tryGet(`/api/forecast/chamber/${chamber}`); }
  scorecard()          { return this.tryGet('/api/forecast/scorecard'); }
  pollsterRatings()    { return this.tryGet('/api/pollsters/ratings'); }
  pollsterRating(id)   { return this.tryGet(`/api/pollsters/${id}/rating`); }
  fairness(dvId)       { return this.tryGet(`/api/districts/${dvId}/fairness`); }
  elections(state)     { return this.tryGet('/api/elections', state ? { state } : {}); }
  officeholders(fips)  { return this.tryGet(`/api/officeholders/${fips}`); }
  racesByPhase(f)      { return this.tryGet('/api/races', f); } // pass {phase:'primary'|'all',...}
  audit(metricId)      { return this.tryGet(`/api/audit/${metricId}`); }
  counterfactual(raceId, scenario) { return this.tryGet('/api/counterfactual', { race_id: raceId, scenario }); }

  /* ----- influence ledger (v3.0) ----- */
  lobbies(filters = {}) { return this.tryGet('/api/lobbies', filters); }
  lobby(id)             { return this.tryGet(`/api/lobbies/${id}`); }

  /* ----- open-data export (used for Brier trend sparklines) ----- */
  exportTable(table, limit = 5000) {
    return this.tryGet(`/api/export/${table}`, { format: 'json', limit });
  }

  /* ----- analyst ----- */
  analystQuery(body, opts) { return this.post('/api/analyst/query', body, opts); }

  /* ----- settings / managed API keys (stored in .env, never the DB) ----- */
  settingsKeys() { return this.tryGet('/api/settings/keys'); }
  /** POST returns {ok, detail, configured} on BOTH 200 and 400 — the 400 body
      still carries the live-validation `detail`, so we read the JSON either way
      instead of throwing it away like post() would. */
  async settingsSaveKey(name, value) {
    let res;
    try {
      res = await fetch(this._url('/api/settings/keys'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, value }),
      });
    } catch (e) {
      return { ok: false, detail: 'backend unreachable — key was not saved', configured: false };
    }
    try { return await res.json(); }
    catch (e) { return { ok: false, detail: `save failed (HTTP ${res.status})`, configured: false }; }
  }

  /* ----- election night ----- */
  electionNightLive(raceId) { return this.tryGet('/api/electionnight/live', { race_id: raceId }); }
  electionNightCall(body)   { return this.post('/api/electionnight/call', body); }

  /* ----- map ----- */
  mapValues(mode, tier, extra = {}) { return this.tryGet('/api/map/values', { mode, tier, ...extra }); }
  mapPins() { return this.tryGet('/api/map/pins'); }

  /* ----- feed fallback & stories ----- */
  stories(sinceIso) { return this.tryGet('/api/stories', { since: sinceIso }); }
  story(id)         { return this.tryGet(`/api/stories/${id}`); }

  /* ----- daily briefing ----- */
  briefingLatest() { return this.tryGet('/api/briefings/latest'); }

  /* ----- watchlist (POST/DELETE via post; 404 anywhere -> feature hidden) ----- */
  watchlist()                          { return this.tryGet('/api/watchlist'); }
  watchlistAdd(entityType, entityId)   { return this.post('/api/watchlist', { entity_type: entityType, entity_id: entityId }); }
  watchlistDelete(entityType, entityId){ return this.post('/api/watchlist/delete', { entity_type: entityType, entity_id: entityId }); }

  /* ----- static boundary data (same-origin vendored files) ----- */
  async staticJson(path) {
    try {
      const res = await fetch(path);
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }
}
