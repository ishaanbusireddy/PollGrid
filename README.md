# PollGrid

Repository: https://github.com/ishaanbusireddy/PollGrid

A real-time US political intelligence platform: primary-source polling, campaign
finance, official election results, legislative activity, and campaign rhetoric —
mapped against demographics from the nation down to the precinct, correlated across
races, reasoned over by a local Ollama-powered Analyst that never touches the actual
math, and rendered on a hand-built WebGL2 map.

**Zero-install.** Python 3.10+ standard library only. No pip install, no npm, no
Postgres.

```bash
python run.py                      # database created, sources seeded, ingestion running
python scripts/seed_demo.py        # optional: synthetic demo data (all tagged, purgeable)
python scripts/purge_synthetic.py  # remove every trace of synthetic data, one command
python -m unittest discover -s tests
```

Open http://localhost:8811/.

## The non-negotiables (see docs/pollgrid_clean_slate_architecture.pdf)

1. **Primary sources only** — pollster releases, OpenFEC, Census ACS, state SoS feeds,
   Congress.gov, OpenElections/VEST. Aggregators are linked to, never scraped.
2. **Race calls are never automated.** The election-night engine flags CALLABLE and
   stops. `AUTO_PUBLISH_CALLS = False` is hardcoded in `modeling/race_calling.py`;
   the schema rejects `called_by` values like `system`.
3. **Append-only everywhere** — averages, fundamentals, forecasts, volatility are
   dated `as_of` snapshots, never overwritten.
4. **Hash-chained provenance** — every poll, fact, and prediction chains to the
   previous row (`scripts/verify_provenance.py` proves our copy wasn't altered; it
   never claims the underlying source was true).
5. **The LLM is bounded** — Ollama-first with cloud fallbacks; it writes narratives,
   dossiers, and rubric scores. It never touches averaging, fundamentals, forecasting,
   the ensemble weights, correlation thresholds, volatility, or a race call. Every
   generative feature has a deterministic fallback the pipeline actually exercises.
6. **Every tunable lives in `config.yaml`** — schema-validated at boot, fails loudly.
7. **A failing source degrades, never crashes** — thread-per-source, capped backoff,
   ok/degraded/down health.
8. **Synthetic data is tagged and purgeable in one operation.**

## Layout

| path | owns |
|---|---|
| `config.yaml` | every tunable |
| `core/` | config loader + validator, SQLite session/locking, hash-chain provenance |
| `domain/` | five-tier geography (seeded: 56 states/territories, 441 districts, 3,152 county-equivalents incl. both CT vintages), parties/candidates (curated floor → API sync → cached AI fill), races + search profiles |
| `ingestion/` | scheduler + one adapter per source; official results in three honest tiers (native / OpenElections / gated AP) + tagged manual entry |
| `processing/` | extraction (classify → canonicalize → geocode → chain) and story clustering |
| `modeling/` | averaging, fundamentals, ideology, forecasting + the Brier backtest gate, chamber Monte Carlo, correlation, volatility (z+CUSUM), coalition regression, counterfactuals, corroboration, rhetoric, race calling, pollster scorecard, fairness metrics, audit trail, the factors taxonomy + elastic-net genius ensemble |
| `analyst/` | context packs (lazy rebuild, invalidate-on-sync) + Ollama-first grounded Q&A |
| `api/` | hand-rolled router, REST routes (every read takes `as_of`), hand-rolled RFC 6455 WebSocket, static serving |
| `frontend/` | buildless ES-module SPA — WebGL2 globe with 2D-canvas and list fallbacks, map builder, polls window, election night mode, the Analyst, themes, Web Audio sound |
| `scripts/` | boundary build, demo seed, synthetic purge, provenance verify, history backfill |
| `tests/` | `python -m unittest discover` |

API keys (all optional; missing keys degrade the source, they never crash it) go in
`.env`: `CENSUS_API_KEY`, `FEC_API_KEY` (DEMO_KEY used otherwise), `CONGRESS_GOV_API_KEY`,
`AP_ELECTIONS_API_KEY` (adapter additionally gated off in config), `ANTHROPIC_API_KEY` /
`GROQ_API_KEY` / `OPENROUTER_API_KEY` (LLM fallbacks behind local Ollama).

Design docs: `docs/pollgrid_clean_slate_architecture.pdf` (the build manual) and
`docs/ARCHITECTURE_REVIEW.md` (the review whose schema fixes this build implements).
