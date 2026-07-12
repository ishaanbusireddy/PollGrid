"""Boot-time config validation. Wrong type, out-of-range value, or unknown key
all raise ConfigError (a SystemExit) before migrate() ever runs."""
from __future__ import annotations

from typing import Any


class ConfigError(SystemExit):
    pass


# (type, min, max) for leaves; dict for nested mappings; ("list", elem_type) for lists.
SCHEMA: dict = {
    "server": {"port": (int, 1, 65535), "host": (str, None, None)},
    "database": {"path": (str, None, None)},
    "ingestion": {
        "intervals_seconds": {
            k: (int, 5, 604800)
            for k in ("polls", "fec", "census", "congress_gov", "news_rss",
                      "targeted_search", "markets", "social", "results_native")
        },
        "election_night": {"results_native_seconds": (int, 5, 3600)},
        "ap_elections": {"enabled": (bool, None, None), "api_key_env": (str, None, None)},
        "resilience": {
            "backoff_multiplier": (float, 1.0, 10.0),
            "max_backoff_seconds": (int, 60, 86400),
            "down_after_failures": (int, 1, 100),
        },
        "budgets": {k: (int, 1, 1000000) for k in ("census", "fec", "congress_gov", "targeted_search", "lda")},
        "news_recency_hours": (int, 1, 720),
        "debate_window": {"poll_seconds": (int, 30, 3600)},
        "social_x": {"enabled": (bool, None, None), "api_key_env": (str, None, None),
                     "handles": ("list", str)},
    },
    "polling": {
        "averaging": {
            "recency_half_life_days": (float, 1, 365),
            "house_effect_adjustment": (bool, None, None),
            "min_sample_size": (int, 0, 100000),
            "weight_by_sample_size": (bool, None, None),
            "herding_discount": (bool, None, None),
            "herding_window_days": (int, 1, 14),
        },
        "regional_ratings_min_graded": (int, 1, 1000),
    },
    "fundamentals": {
        "weights": {
            k: (float, 0.0, 1.0)
            for k in ("incumbency", "generic_ballot", "economic_index", "partisan_lean", "fundraising_ratio")
        },
        "competitiveness_bands": {
            "tossup_max": (float, 0.0, 1.0), "lean_max": (float, 0.0, 1.0), "likely_max": (float, 0.0, 1.0),
        },
    },
    "correlation": {
        "same_window_similarity_threshold": (float, 0.0, 1.0),
        "historical_similarity_threshold": (float, 0.0, 1.0),
        "same_window_max_gap_hours": (int, 1, 720),
    },
    "volatility": {"z_window": (int, 3, 120), "cusum_k": (float, 0.0, 5.0), "cusum_h": (float, 0.5, 20.0)},
    "forecasting": {
        "enabled": (bool, None, None),
        "brier_ceiling": (float, 0.0, 1.0),
        "min_graded_predictions": (int, 1, 10000),
        "auto_enable_earned": (bool, None, None),
        "margin_scale": (float, 0.5, 20.0),
        "fundamentals_weight_with_polls": (float, 0.0, 1.0),
    },
    "election_night_calling": {"callable_margin_factor": (float, 0.5, 5.0)},
    "chamber_simulation": {"n_sims": (int, 100, 2000000), "national_shock_sd": (float, 0.0, 0.5),
                           "senate_not_up_dem": (int, 0, 100), "senate_not_up_rep": (int, 0, 100)},
    "genius_layer": {
        "ensemble_method": (str, None, None),
        "refit_interval_days": (int, 1, 365),
        "l1_ratio": (float, 0.0, 1.0),
        "alpha": (float, 0.0, 100.0),
        "context_pack_token_budget": (int, 1000, 250000),
        "event_rescore_min_gap_minutes": (int, 1, 1440),
    },
    "llm_provider": {
        "primary": (str, None, None),
        "fallback_order": ("list", str),
        "ollama": {
            "default_model": (str, None, None),
            "fallback_model": (str, None, None),
            "upgrade_model": (str, None, None),
            "host": (str, None, None),
        },
    },
    "analyst": {"queue_lanes": (int, 1, 4)},
    "demographics": {"sync_cadence_days": (int, 1, 365), "min_population_threshold": (int, 0, 10**9)},
    "election_night": {"auto_publish_calls": (bool, None, None)},
    "synthetic": {"allow_seed_demo": (bool, None, None)},
}


def _check(node: Any, schema: Any, path: str, errors: list[str]) -> None:
    if isinstance(schema, dict):
        if not isinstance(node, dict):
            errors.append(f"{path}: expected mapping, got {type(node).__name__}")
            return
        for key in node:
            if key not in schema:
                errors.append(f"{path}.{key}: unknown key")
        for key, sub in schema.items():
            if key not in node:
                errors.append(f"{path}.{key}: missing")
            else:
                _check(node[key], sub, f"{path}.{key}", errors)
        return
    if isinstance(schema, tuple) and schema[0] == "list":
        if not isinstance(node, list) or not all(isinstance(x, schema[1]) for x in node):
            errors.append(f"{path}: expected list of {schema[1].__name__}")
        return
    typ, lo, hi = schema
    ok_type = isinstance(node, typ) or (typ is float and isinstance(node, int) and not isinstance(node, bool))
    if typ is not bool and isinstance(node, bool):
        ok_type = False
    if not ok_type:
        errors.append(f"{path}: expected {typ.__name__}, got {type(node).__name__} ({node!r})")
        return
    if lo is not None and node < lo:
        errors.append(f"{path}: {node} below minimum {lo}")
    if hi is not None and node > hi:
        errors.append(f"{path}: {node} above maximum {hi}")


def validate_config(config: dict) -> None:
    errors: list[str] = []
    _check(config, SCHEMA, "config", errors)
    if not errors:
        weights = config["fundamentals"]["weights"]
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-9:
            errors.append(f"config.fundamentals.weights: must sum to 1.0, got {total}")
        if config["election_night"]["auto_publish_calls"] is not False:
            errors.append("config.election_night.auto_publish_calls: must be false; "
                          "automated race calls are forbidden (hardcoded in modeling/race_calling.py)")
    if errors:
        raise ConfigError("config.yaml failed validation:\n  " + "\n  ".join(errors))
