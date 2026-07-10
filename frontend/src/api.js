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

  async post(path, body) {
    let res;
    try {
      res = await fetch(this._url(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (e) { throw new ApiError(0, 'network unreachable'); }
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
  audit(metricId)      { return this.tryGet(`/api/audit/${metricId}`); }
  counterfactual(raceId, scenario) { return this.tryGet('/api/counterfactual', { race_id: raceId, scenario }); }
  volatility(scope = 'national')   { return this.tryGet('/api/volatility', { scope }); }

  /* ----- analyst ----- */
  analystQuery(body) { return this.post('/api/analyst/query', body); }

  /* ----- election night ----- */
  electionNightLive(raceId) { return this.tryGet('/api/electionnight/live', { race_id: raceId }); }
  electionNightCall(body)   { return this.post('/api/electionnight/call', body); }

  /* ----- map ----- */
  mapValues(mode, tier, extra = {}) { return this.tryGet('/api/map/values', { mode, tier, ...extra }); }
  mapPins() { return this.tryGet('/api/map/pins'); }

  /* ----- feed fallback ----- */
  stories(sinceIso) { return this.tryGet('/api/stories', { since: sinceIso }); }

  /* ----- static boundary data (same-origin vendored files) ----- */
  async staticJson(path) {
    try {
      const res = await fetch(path);
      if (!res.ok) return null;
      return await res.json();
    } catch (e) { return null; }
  }
}
