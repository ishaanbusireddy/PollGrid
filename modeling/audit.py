"""The public audit trail: every average/forecast number gets a metric_id whose
itemized computation (inputs, formula, output) is one GET away. Radical
transparency instead of a black box you're asked to trust."""
from __future__ import annotations

import json

from core import db
from core.util import new_metric_id, now_iso


def record(metric_type: str, scope: str, formula: str, inputs, output) -> str:
    metric_id = new_metric_id(metric_type)
    db.execute(
        "INSERT INTO computation_audit_log(metric_id,created_at,metric_type,scope,formula,inputs_json,output_json) "
        "VALUES(?,?,?,?,?,?,?)",
        (metric_id, now_iso(), metric_type, scope, formula,
         json.dumps(inputs, default=str), json.dumps(output, default=str)))
    return metric_id
