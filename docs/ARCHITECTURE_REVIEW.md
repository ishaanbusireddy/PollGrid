# PollGrid Clean-Slate Architecture — Review

**Reviewing:** `docs/pollgrid_clean_slate_architecture.pdf` (Draft 001, 40 pp.)
**Reviewed against:** the GlobeGrid reference codebase (the document's stated engineering baseline) and external data-source ground truth (Census FIPS reference, ACS table catalog, FEC/Congress.gov API behavior).

---

## Verdict

The document is a strong, unusually honest draft: the hard constraints (no LLM in the math path, no automated race calls, append-only provenance, primary sources only) are the right spine, the tiered official-results strategy fixes the AP-access fantasy of earlier drafts, and most external claims check out against real APIs and real datasets. **It is approvable as a build plan after revisions.** The blocking items are concentrated in the geography schema — the versioned-district primary key is incompatible with the polymorphic tables that must reference it, Connecticut's 2022 county-equivalent change is unrepresentable, and there is no precinct→district crosswalk even though Maine/Nebraska elector math and all precinct roll-ups depend on one — plus two scoped-down-but-not-scoped claims (a poll-dependent backtest "to 1950" without any historical polling source, and a genius-layer gate that cannot accumulate training pairs until years after launch).

Nothing below invalidates the shape of the design. Every issue has a local fix.

---

## 1. What is genuinely strong

- **The LLM boundary is stated once and is testable.** The allowed/never-allowed split (§11) is enforceable file-by-file, the neutral-0 scorecard fallback degrades to quantitative-only instead of blocking, and the devil's-advocate rank-comparison rule keeps the model from grading itself.
- **The tiered results strategy is honest.** Treating AP as a gated, default-off paid upgrade and building on state feeds + OpenElections/VEST — with the UI labeling which tier is live — is the correct call, and the earlier draft's biggest reality gap is explicitly named and fixed.
- **The backtest gate.** Forecasts earning visibility per race-type via a Brier ceiling over a minimum graded count, with the failing categories shown on a public scorecard, is a real differentiator and correctly kept out of LLM hands.
- **Vintage-awareness is designed in, not bolted on.** Redistricting-versioned district geometries, ACS Congress-vintage tagging, boundary-aware history panels, and confidence tags applied once at import are all correct instincts (the schema just doesn't fully deliver them yet — see §3).
- **The concrete ingestion section is largely accurate.** Spot-checked and confirmed: ACS 5-year geography clauses (including `congressional%20district`), CVAP as a separate special-tabulation product rather than a B-table, OpenFEC endpoints and DEMO_KEY/1,000-per-hour key tiers, Congress.gov House roll-call coverage starting with the 118th Congress with VoteView filling history, OpenElections/VEST provenance, and the Maine/Nebraska `elector_method` modeling. The URL-encoding arithmetic for map scenarios (435 × 3 bits → 224 base64url chars) is also correct.
- **Lazy context-pack rebuilds.** Invalidate-on-sync + rebuild-on-demand genuinely dissolves the rebuild-storm problem rather than rate-limiting it.

## 2. Factual corrections (document errors)

### 2.1 DC is in the presidential electoral math — BLOCKING (one sentence)
The geography-tier table says DC and the territories are "tracked for context, not electoral math." Wrong for DC: since the 23rd Amendment, DC casts 3 electoral votes and every presidential model must include it. The DDL itself is fine (DC gets `elector_method='winner_take_all'`, `electoral_votes=3`); the prose must be corrected before it becomes a filter in code — a `WHERE is_state` guard written from that sentence silently drops 3 EVs.

### 2.2 Connecticut broke the county model in 2022 — BLOCKING
Census replaced Connecticut's 8 legacy counties with 9 planning regions as county-equivalents effective 2022; ACS releases and TIGER/Line files from that vintage forward carry planning-region GEOIDs (09110–09190). Consequences for the schema as written:

- `county_equivalents.type CHECK (... 'county','parish','borough','census_area','independent_city')` rejects the required `planning_region` type.
- `county_equivalents` has **no boundary/effective versioning** (unlike districts), so post-2022 Census pulls and pre-2022 election results for CT cannot both be represented, let alone joined. County-equivalents change elsewhere too (Valdez–Cordova AK split in 2019; consolidations the doc's own 3,140–3,150 tolerance nods to).
- The Phase-A count check was validated against the pre-2022 reference (8 CT counties → 3,143); the current vintage has 9 CT planning regions.

Fix: add `planning_region` (and Alaska's `municipality`/`city_and_borough` if types mirror Census labels exactly) to the CHECK, and give `county_equivalents` the same `effective_from`/`effective_to` treatment districts get. This is not gold-plating — the 1950 archive requires historical county-equivalents regardless (Virginia independent-city mergers, Alaska reorganizations, South Dakota's Shannon→Oglala Lakota rename).

### 2.3 Territories vs. the Phase-A count check — inconsistent as written
`states` holds 56 rows including 5 territories, and the Phase-A referential-integrity check requires every `county_equivalents.state_fips` to appear in `states`. But Puerto Rico alone has 78 municipios as Census county-equivalents; loading territory county-equivalents blows the 3,140–3,150 check, while not loading them leaves territories with no tier below state (so no county/district demographics for the "tracked for context" promise). The FIPS reference used for the count contains exactly 3,143 rows for the 50 states + DC and zero territory rows — so the check as stated is implicitly "states + DC only." Say so explicitly, or widen the range and include municipios.

### 2.4 Static `elector_method` and `electoral_votes` columns contradict the historical archive
Electoral-vote counts change at every apportionment, Maine has split by district only since 1972 and Nebraska since 1992 — yet `states.elector_method` and `states.electoral_votes` are single static columns, while a separate `electoral_vote_allocations` table also exists in the grouped list. Two homes for one fact, and the static columns are wrong for any cycle but the current one — against an archive imported to 1789. Fix: make `electoral_vote_allocations` (keyed by state + cycle) the single source of truth; if the static columns stay for convenience, document them as "current cycle cache, never read by historical queries."

### 2.5 `political_history` cannot represent a double-barreled Senate year
`UNIQUE (tier, entity_id, office, cycle_year)` collides whenever a state holds both a regular and a special Senate election in the same cycle — Georgia 2020, Arizona 2020, Minnesota 2018, and many more back to 1950. The prose says specials are "their own tagged event type," but the schema has no seat/class/special column to hang the tag on. Fix: add `seat` (Senate class or `special`) to the table and the UNIQUE key. The same review should decide how runoffs (GA general runoffs) key.

### 2.6 "Exactly 435 current rows" vs. non-voting delegate districts
The domain model says the district tier covers "435 voting + non-voting delegates," but the Phase-A done-criterion demands **exactly 435** rows with `effective_to IS NULL`. If delegate districts (DC, PR, and four territories — six delegate seats) are in the table, the current-vintage count is 441. Pick one: either delegates live in the table (change the check to 435 voting + 6 non-voting, with an `is_voting` flag) or they don't (change the domain-model prose).

### 2.7 ACS language table: verify `B16001` availability
Most listed ACS tables are correct as stated (B01003, B01001, B02001, B03002, B15003, B19013, B19001, B23025, B25003, B05002, B21001, B18101). `B16001` (detailed language spoken at home) is the exception worth a vintage check before Phase B: recent ACS releases publish it for limited geographies only, with the collapsed `C16001` as the broadly-available variant. Cheap to confirm against the live variables endpoint during Phase B; noted here so it doesn't surprise the district/county pulls.

### 2.8 The backtest "to 1950" is only true for the fundamentals path
The nightly forecast backtest "replayed against the archive back to 1950" needs the model's *inputs* as of each historical date. Certified results to 1950: yes, that's the archive. But there is **no historical polling ingestion anywhere in the plan** — poll adapters ingest current releases, and the major historical poll archives (e.g. Roper) are licensed products excluded by the primary-source budget. So poll-average-dependent forecasts can only be graded on races that occur after ingestion begins; only the fundamentals-only model can be replayed to 1950. The gate still works — it just means poll-blended categories earn visibility from live cycles, slowly. The document should scope the claim rather than let readers assume a 75-year graded track record at launch.

### 2.9 The genius layer's training set starts at ~zero — and stays small for years
Same structural issue, stated more strongly because the doc leans on it: elastic-net weights are fit on graded Factor-Scorecard→outcome pairs, but factor scores are LLM-scored from *contemporaneously ingested* text. There is no mechanism to produce cited factor scores for 1994 races. Per race-type category, meaningful pairs accrue at a couple of cycles per presidential term. The design already degrades gracefully (quantitative-only until the gate is beaten), so nothing breaks — but the document should state plainly that the qualitative-augmented forecast is expected to be dormant for its first several cycles, or add a retrospective-scoring pathway (scoring archived coverage from the news archive as it deepens) as explicit future work.

## 3. Schema defects (beyond the factual items above)

### 3.1 BLOCKING — the versioned-district PK breaks every polymorphic reference to it
`congressional_districts` has composite PK `(geoid, congress_number)` — correct for versioning. But `demographics.entity_id` and `political_history.entity_id` are single TEXT columns holding "the relevant tier's own PK." A composite PK doesn't fit in one column, so as written:

- The §05 vintage-matching promise ("a demographic row is only ever joined to the district-shape version it actually describes") is **unimplementable** — `entity_id='0601'` cannot say which boundary version it describes.
- `UNIQUE (tier, entity_id, as_of, category, variable)` would collide/dedupe rows that legitimately differ only by boundary vintage.

Fix (pick one, state it in the DDL): a surrogate `district_version_id INTEGER PRIMARY KEY` on `congressional_districts` with `(geoid, congress_number)` UNIQUE, and polymorphic tables reference the surrogate; or a canonical encoded key (`geoid || '@' || congress_number`) documented as *the* PK format. The surrogate is cleaner and keeps the GEOID-as-natural-key benefit via the UNIQUE index.

### 3.2 BLOCKING — no precinct→district crosswalk exists anywhere in the schema
`precincts` reference only `county_geoid`. But: Maine/Nebraska presidential math "reads off the district tier"; precinct results must roll up to districts for any district-level analysis; districts cut across counties (the doc says so itself, in the map-overlay section); and precincts can split across districts. A `precinct_district_assignments` table (precinct × district-version, with a split-fraction column for the rare straddler) is required infrastructure, not an enhancement — Phase A/B is the right time to add it, since it's built from the same TIGER geometry work the offline pipeline already does.

### 3.3 Precincts are unversioned, but VTD boundaries change every cycle
VEST deposits are per-cycle precisely because precinct lines are redrawn constantly. A single unversioned `precincts` row cannot carry 2016, 2020, and 2024 geometries. Give precincts a `cycle_year` (or effective range) — this also disambiguates which boundary a `demographics` areal-interpolation row was computed against.

### 3.4 Polymorphic tables have no enforceable referential integrity
SQLite cannot FK a polymorphic `(tier, entity_id)` pair. The Phase-A integrity query covers counties→states only. Extend the same pattern: a small set of standing integrity queries (or triggers) covering every polymorphic table (`demographics`, `political_history`, `context_packs`, `qualitative_factor_scores`), run by the same nightly job that runs the backtest. Cheap, and it's the only referential enforcement those tables will ever have.

### 3.5 One datum, two homes: `political_historical` demographics vs. `political_history`
The demographics categories include "Political-historical (…past results back to 1950)" while `political_history` is its own table. Declare `political_history` the owner of results/turnout and keep only registration mix in `demographics` — otherwise two pipelines will disagree about the same number, which is exactly what the one-fact-chain moat is supposed to prevent.

## 4. Architectural risks (design holds, envelope needs stating)

### 4.1 SQLite single-writer vs. a large writer population — serialize the hash chain explicitly
Thread-per-source means on the order of 60+ writer threads (a dozen pollsters, FEC, Census, Congress, transcripts, RSS, markets, social, targeted-search profiles, per-state SoS adapters), plus nightly refits and lazy context-pack builds. WAL mode gives concurrent readers but still **one writer at a time** — fine at these write rates, but two disciplines must be stated, not assumed: a single-writer lock (or write queue) shared by all threads with `busy_timeout` as backstop; and, harder, **hash-chain appends must be serialized per chain** — two threads reading the same "previous row hash" and both inserting produces a forked chain that verification will flag forever. The verification CLI should also be the thing that catches this in CI.

### 4.2 Google News RSS at race-profile scale will get throttled
~470+ tracked races (435 House + ~34 Senate + governors + president) × per-query RSS every 600s ≈ 40k+ queries/day against an endpoint with no contract and aggressive anti-bot behavior. The design already has the right primitive — the daily budget counter with "budget exhausted → still `ok`, flat interval" — but §15 doesn't apply it. Make targeted search explicitly budget-capped with competitiveness-weighted rotation (competitive races hourly, safe seats daily), and treat HTTP 429/captcha responses as `degraded`, not retry-tighter.

### 4.3 Ollama "preemption" is queue-jump, not preemption
One generation at a time per loaded model means an interactive user still waits behind an in-flight background rebuild for up to a full generation. Either cancel in-flight background generations on interactive arrival (Ollama supports request cancellation by dropping the connection), or state the honest latency envelope ("interactive answers may queue up to N seconds behind a rebuild"). The two-lane queue as described only reorders the waiting room.

### 4.4 `demographics` table sizing — fine, but say so
~170k precincts × ~40 variables × annual snapshots ≈ 7M rows/year plus states/counties/districts, dominated by the five-column UNIQUE index. SQLite handles this; the doc should state expected scale and confirm the areal-interpolation build writes precinct demographics in bulk transactions (not row-at-a-time through the single writer).

### 4.5 Election night on `http.server` + hand-rolled WebSocket — state the concurrency envelope
The zero-install pledge is worth the trade, but election night is the one night everything spikes at once: results ingestion at 30s intervals, WebSocket fan-out, context-pack invalidations, and page loads. A stated target (e.g. "designed for N concurrent WebSocket clients; beyond that, degrade to REST polling") turns an unknown into a test.

## 5. Internal inconsistencies & cross-reference nits

1. **Dangling cross-reference:** `results_native: 30  # tightens automatically on election night, see §12` — §12 is the map chapter; no section specifies the tightening mechanism (what triggers "election night mode" for the scheduler, what the tightened interval is). Small, but it's the one scheduler behavior with no spec.
2. **House-effect ordering:** poll averaging (Phase F, wave 1) is "house-effect-adjusted" with `house_effect_adjustment: true` by default, but the pollster ratings that *set* house-effect weights land in Phase EE (wave 2). Wave-1 averages therefore run unadjusted or need an interim weight source. Either default the flag off until EE, or seed provisional ratings in wave 1 — say which.
3. **Correlation phase note:** Phase M's parenthetical says the historical-analog archive "landed in phase D — deepened later in phase AA," which is right, but same-window correlation depends on the embedding stage that no lettered phase explicitly owns; presumably M itself — one clause would fix it.
4. **`config.yaml` fundamentals weights** sum to exactly 1.0 as printed (0.20+0.25+0.20+0.25+0.10) — consistent with the validator claim. ✓ (Noted so reviewers don't re-derive it.)
5. **Scenario-encoding math** checks out (435×3=1,305 bits → 164 bytes payload + 4 header → 224 base64url chars). ✓

## 6. Claims verified against the GlobeGrid codebase

The document's core rhetorical move is "built the way GlobeGrid was actually built." Verified against the GlobeGrid source:

| # | Claimed inheritance | Verdict | Evidence in GlobeGrid |
|---|---|---|---|
| 1 | Thread-per-source scheduler: daemon thread per source, re-reads its DB row each tick, shared stop-event (not sleep), exponential backoff capped by config, ok/degraded/down health, "missing key → degraded forever, flat", "budget exhausted → still ok, flat" | **Confirmed** | `ingestion/scheduler.py` — every behavior present exactly as the doc describes, including the budget-exhaustion-is-not-a-failure rule (`ingestion/budget.py`) |
| 2 | Hash-chained provenance with a verification CLI, append-only | **Confirmed** | `processing/provenance.py` (SHA-256 chain over facts and predictions, prev-hash linkage, head cached for O(1) append) + `scripts/verify_provenance.py`; same honest scope note ("proves our copy wasn't altered, not that the source was true") |
| 3 | Single `config.yaml` + `.env`, schema-validated at boot, fails loud | **Confirmed** | `config_schema.py` type+range schema, `validate_config` raises before `migrate()` runs. Minor nuance: GlobeGrid also supports an optional gitignored `config.local.yaml` overlay |
| 4 | Stdlib-first: `http.server` + hand-rolled router, hand-rolled RFC 6455 WebSocket, SQLite WAL, no ORM | **Confirmed** | `main.py` (ThreadingHTTPServer), `api/router.py`, `websocket/feed_socket.py`; `db/session.py` uses a **single global writer RLock with a 20s timeout + `busy_timeout=8000`** — this is the write discipline §4.1 above says PollGrid must restate, because it exists only as GlobeGrid convention, not in the PollGrid doc |
| 5 | Three-tier acquisition (curated floor / additive API sync that never overwrites / cached background LLM gap-fill) | **Partial** | Floor, `INSERT OR IGNORE` + COALESCE-style sync, and non-blocking cached gap-fill all confirmed (`geopolitics/sync.py`, `processing/bg_synth.py`). Divergence: GlobeGrid's "cited sources" are docstring-level prose, **not a per-row citation column** — PollGrid's curated floor promises cited rows, so it needs a `source`/citation field GlobeGrid never actually had |
| 6 | LLM boundary: Ollama-first fallback chain, prose-only, strict JSON + one retry + deterministic template, devil's-advocate downgrade-only via rank comparison | **Confirmed** | `processing/llm.py`, `causal_link.py` — the rank-comparison guard and the exercised deterministic fallback both exist verbatim as described |
| 7 | Buildless frontend: WebGL2 globe (no three.js), 3-tier step-down, terminator/city lights/fly-to/particle threads, canvas-crop PNG snapshot, command palette, slide pane, Web Audio synthesis with convolution reverb + ADSR, one-accent-variable theming | **Partial** | All confirmed in `frontend/src/components/` **except camera bookmarks**: GlobeGrid's bookmarks are server-side story/entity bookmarks; named-and-dated *camera* bookmarks kept in the browser do not exist. PollGrid's map-builder saving (§14) cites "the same way GlobeGrid's camera bookmarks do" — that precedent is missing, so the scenario-saving localStorage layer is new work, not a port |
| 8 | Volatility index as "z-score plus CUSUM"; embedding-threshold correlation (same-window + historical-analog); story-cluster WS push (never raw articles) with REST fallback; as_of scrubber | **Confirmed, one phrasing nuance** | z-score + CUSUM exist in `processing/anomaly.py` as changepoint detectors layered **on top of** a weighted-sum index (`instability.py`) — they are not the index formula itself. PollGrid's §07 wording inherits the same slight overstatement |
| 9 | GDELT retired for low-quality volume | **Confirmed** | `scheduler.py` hard-blocks GDELT with the owner's comment quoted; `main.py` purges GDELT rows on every boot. The §15 exclusion rationale is real history |
| 10 | Zero-install pledge: `python run.py` and it's up | **Confirmed** | `run.py`; `requirements.txt` declares zero required third-party packages, optional deps auto-detected |

**Takeaway:** the inheritance claims are honest — 8 of 10 fully hold in code. The two gaps matter to PollGrid specifically: (a) the curated floor's *structured* citation column is a new requirement, not a port, and should appear in the §19 DDL for `candidates`/`officeholders`; (b) client-side scenario persistence has no GlobeGrid precedent to copy and should be sized as fresh work in Phase FF. Additionally, GlobeGrid's single-writer-lock discipline (`db/session.py`) is the exact mechanism §4.1 asks the PollGrid doc to make explicit — it exists in the parent codebase but nowhere in this document.

## 7. Recommended revisions before Phase A starts

**Blocking (schema-correctness — cheap now, migrations later):**
1. Fix the DC electoral-math sentence (§2.1).
2. Resolve the versioned-district PK vs. polymorphic `entity_id` contradiction with a surrogate `district_version_id` (§3.1).
3. Add `planning_region` to the county type CHECK and effective-dating to `county_equivalents`; re-state the Phase-A count check against the current Census vintage and explicitly scope territories in or out (§2.2, §2.3).
4. Add a precinct→district-version crosswalk table and a `cycle_year` on precincts (§3.2, §3.3).
5. Add `seat`/`special` to `political_history` and its UNIQUE key (§2.5).
6. Reconcile "exactly 435" with delegate districts (§2.6).

**Non-blocking (scope honesty — edit the prose, keep the design):**
7. Scope the backtest-to-1950 claim to the fundamentals path; state that poll-blended and qualitative-augmented categories earn their gates from live cycles only (§2.8, §2.9).
8. Make `electoral_vote_allocations` the single source of truth for EV counts and elector method by cycle (§2.4).
9. Apply the budget-counter discipline explicitly to targeted Google News search with competitiveness-weighted rotation (§4.2).
10. Specify the single-writer/hash-chain serialization discipline (§4.1), the election-night interval-tightening mechanism (§5.1), and the wave-1 house-effect interim behavior (§5.2).

With items 1–6 folded into the §19 DDL and the four prose scopings applied, this document is a sound basis to start Phase A.
