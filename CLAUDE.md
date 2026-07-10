# PollGrid — agent notes

- Zero-dependency rule: stdlib only. Never add a package to fix a problem; optional
  deps (spacy, sentence-transformers) are auto-detected with deterministic fallbacks.
- Run: `python run.py --no-browser` (port 8811). Tests: `python -m unittest discover -s tests`.
- Demo data: `python scripts/seed_demo.py`; remove with `python scripts/purge_synthetic.py`.
  Anything synthetic MUST set is_synthetic=1.
- All writes go through core.db.write() (single global writer lock). Rows in polls /
  extracted_facts / predictions may ONLY be inserted via core.provenance.chained_insert.
- The LLM boundary (manual §11) is enforced by import discipline: nothing under
  modeling/ may import analyst.llm for anything that produces a number the platform
  stores — prose, rubric scores, and narratives only, always with a deterministic
  fallback. Race calls are human-only; do not add any automated call path.
- config.yaml is the single home for tunables; add new keys to core/config_schema.py
  or boot fails (that's the point).
- API surface is frozen in docs/API_CONTRACT.md — frontend and backend both code to it.
- Every average/forecast must write a computation_audit_log row (modeling/audit.record).
