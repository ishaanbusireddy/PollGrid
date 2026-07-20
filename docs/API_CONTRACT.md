# PollGrid API contract (frozen for the v1 build)

All routes are JSON over GET unless noted. Every read route accepts `?as_of=YYYY-MM-DD`
(snapshot semantics: return the latest row with `as_of <= param`). Errors:
`{"error": "message"}` with 4xx/5xx. Static frontend served at `/`, files under
`frontend/` (SPA entry `frontend/index.html`); boundary data at
`/static/data/us_states.json`, `/static/data/us_counties.json`
(`/static/data/us_districts.json` may 404 until `scripts/build_boundaries.py` runs —
frontend must degrade to "district overlay unavailable").

## Status & meta
- `GET /api/status` → `{sources:[{id,name,source_type,health,last_run_at,last_error,is_active}], counts:{races,polls,candidates,facts,stories}, election_night_mode:bool}`
- `GET /api/config` → the full validated config tree (read-only mirror)
- `GET /api/diagnostics` → `{llm:{provider,model,reachable}, chains:[{table,ok,detail}], integrity:{check:count_of_violations}, db_path, synthetic_rows:int}`

## Geography & demographics
- `GET /api/geo/states` → `[{fips_code,usps_code,name,is_territory,electoral_votes,elector_method}]` (EVs joined from current allocation)
- `GET /api/geo/counties?state=06` → `[{geoid,name,type,state_fips}]` (current vintage)
- `GET /api/geo/districts?state=06` → `[{district_version_id,geoid,district_number,is_voting,congress_number}]` (current: `effective_to IS NULL`)
- `GET /api/demographics/{tier}/{entity_id}` → `{tier,entity_id,as_of,rows:[{category,variable,value,confidence,source}], thin_coverage:bool}`
  - tiers: `nation|state|congressional_district|county_equivalent|precinct`; entity_id: `US`, state fips, district_version_id, county geoid, precinct_id
- `GET /api/entities/{tier}/{id}/history` → `{rows:[{office,seat,cycle_year,winner_party,dem_pct,rep_pct,margin_pct,turnout_pct,confidence}], boundary_events:[{congress_number,effective_from,note}]}`

## Races
- `GET /api/races?cycle=2026&type=senate&state=30&status=live&competitive=1&phase=general` → `[{id,name,race_type,phase,cycle_year,state_fips,district_number,seat,status,competitiveness,election_date,leader_party,leader_margin}]` (all filters optional; `phase` defaults to `general`, accepts `primary`/`runoff`/`all` — v4.3 addition)
- `GET /api/elections?state=06` → `{as_of, entries:[{date,kind,states:[{fips,usps,name}]}], races?:[...]}` — the 2026 calendar (primaries + general), chronological; `state` narrows and adds that state's dated races (v4.3 addition)
- `GET /api/officeholders/{state_fips}` → `{state_fips, governor:{candidate_id,name,party_code,portrait_url,start_date}|null, senators:[...], house:[{district_number,...}]}` — current officeholders, person-level (v4.3 addition)
- `GET /api/races/{id}` → `{race, candidates:[{id,name,party_code,is_incumbent,ideology_score}], average:{as_of,parties:{DEM:48.1,...},n_polls}, fundamentals:{as_of,dem_score,components}, forecast:{model,dem_prob,rep_prob,visible:bool,gate_reason}, narrative:{what_changed,why_it_might_have_changed,what_to_watch,confidence,generated_by}, corroboration:{badge:bool,signals:[...]}, volatility:{score,as_of}}`
- `GET /api/races/{id}/framing` → `{matrix:[{outlet,leaning,topic,framing}], ad_spend:[{sponsor,medium,amount}]}`
- `GET /api/factors/{race_id}` → `{factors:[{key,name,family,method,score,rationale,citations:[fact_id]}], as_of}`
- `GET /api/forecast/ensemble/{race_id}` → `{quantitative:{dem_prob}, ensemble:{dem_prob}|null, live_model:"quantitative"|"ensemble", category, backtest:{brier_quant,brier_ensemble,n_graded}|null}`

## Polls
- `GET /api/polls?race_id=&race_type=&state=&pollster=&population=&from=&to=&limit=100&offset=0` → `{total, rows:[{id,pollster,pollster_grade,race_id,race_name,field_start,field_end,sample_size,population,moe,results:{DEM:47,...},adjusted:{DEM:46.4,...},release_url}]}`
- county pages: call with the county's state + covering district race ids; server never fabricates county polls (scope labels in `race_name`).

## Candidates & parties
- `GET /api/candidates?query=&state=&office=&limit=50` → `[{id,name,party_code,state_fips,office,first_cycle,last_cycle,curated}]`
- `GET /api/candidates/{id}` → `{candidate (all columns incl. bio/positions_summary/citation), races:[…], finance:{total_receipts,total_disbursements,as_of}|null, ideology:{score,components}|null, stances:[{topic,stance,method}]}`
- `GET /api/parties` / `GET /api/parties/{id}` → dossier rows

## Articles
- `GET /api/articles/{entity_type}/{id}?sort=recency|relevance&limit=50` → `[{raw_item_id,title,url,outlet,reliability_tier,published_at}]` (entity_type: race|candidate|party)

## Intelligence
- `GET /api/forecast/chamber/{chamber}` (senate|house|ec) → `{as_of,n_sims,dem_control_prob,seat_distribution:{seats:prob}}`
- `GET /api/forecast/scorecard` → `[{category,model,brier,n_graded,passed,live}]` (every category, including failing ones)
- `GET /api/pollsters/ratings` → `[{id,name,grade,avg_abs_error,n_graded,house_effect_dem,weight_multiplier}]`
- `GET /api/pollsters/{id}/rating` → same row + history
- `GET /api/districts/{district_version_id}/fairness` → `{state_fips,congress_number,efficiency_gap,mean_median,n_districts}`
- `GET /api/audit/{metric_id}` → `{metric_id,metric_type,scope,formula,inputs,output,created_at}`
- `GET /api/counterfactual?race_id=&scenario=dropout:{candidate_id}|turnout:{cycle}` → `{branches:[{label,probs,precedents:[{cycle_year,description}],narrative}], generated_by}`
- `GET /api/volatility?scope=national|race:{id}` → `{score,as_of,components}`

## Analyst
- `POST /api/analyst/query` body `{entity_type,entity_id,question,session_id?}` → `{answer,citations:[{kind,ref,label}],model,session_id,pack_stale:bool}`
  (deterministic fallback answer with `model:"deterministic"` when no LLM reachable — never an error)

## Election night
- `GET /api/electionnight/live?race_id=` → `{races:[{race_id,name,callable,called:{winner_party,called_by,called_at}|null,counties:[{geoid,party_votes:{},pct_reporting}],source_tier,total_votes:{}}]}`
- `POST /api/electionnight/call` body `{race_id,winner_party,called_by,notes}` → 201; rejects called_by in (system/model/auto/ai) with 400. NO automated path exists.

## Map
- `GET /api/map/values?mode={partisan_lean|forecast|average_margin|volatility|turnout|fundraising|demo:{category}:{variable}}&tier={state|county|district}&cycle=&race_type=` → `{mode,tier,values:{entity_key:number},confidence:{entity_key:"measured"|"derived"},legend:{min,max,label}}`
  - entity_key: state fips for tier=state, county geoid for county, district geoid for district
- `GET /api/map/pins` → `[{lat,lon,kind:poll|story|call,label,race_id,ts}]`

## Export
- `GET /api/export/{table}?format=csv|json&limit=` → raw table dump (any table; 404 unknown)

## Stories, briefings & watchlist (v1.1 additions)
- `GET /api/stories/{id}` → `{story:{id,headline,category,race_id,state_fips,score,created_at}, facts:[{id,summary,category,occurred_at,created_at,url,outlet,reliability_tier}], race:{id,name}|null}` — the event breakdown behind one feed card
- `GET /api/briefings/latest` → `{as_of,body,model}` (404 until the nightly job generates one; `model:"deterministic"` when no LLM)
- `GET /api/watchlist` → `[{entity_type,entity_id,label}]`
- `POST /api/watchlist` body `{entity_type,entity_id}` → 201
- `POST /api/watchlist/delete` body `{entity_type,entity_id}` → 200
- `GET /api/demographics/trends/{race_id}` → coalition detector output `{coefficients,r2,n,as_of}` (404 if insufficient county history)

## WebSocket
- `GET /ws/feed` (RFC 6455). Server pushes JSON frames:
  `{type:"story",payload:{story_card}}`, `{type:"poll",payload:{poll_row}}`,
  `{type:"volatility",payload:{scope,score}}`, `{type:"race_call",payload:{race_id,winner_party,called_by}}`,
  `{type:"results",payload:{race_id}}`
- Fallback: frontend polls `GET /api/stories?since=ISO` (`[{id,headline,category,race_id,state_fips,score,updated_at,facts:[{id,summary,category}]}]`) every 15s after socket down 60s.
