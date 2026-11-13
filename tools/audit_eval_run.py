#!/usr/bin/env python3
"""Check a completed GLM skill-evaluation run against its visible tool evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


AMINER_LINK = re.compile(r"https?://(?:www\.)?aminer\.cn/(?:pub|profile)/([0-9a-f]{8,})")
MONEY = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)")
TOTAL_LABEL = re.compile(r"合计|总计|总费用")


def _json_stdout(result: dict) -> dict:
    try:
        value = json.loads(result.get("stdout") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def declared_total(answer: str) -> float | None:
    totals = []
    for line in answer.splitlines():
        if TOTAL_LABEL.search(line):
            amounts = MONEY.findall(line)
            if amounts:
                totals.append(float(amounts[-1]))
    return totals[-1] if totals else None


def audit(record: dict) -> dict:
    answer = record.get("final_answer") or ""
    evidence = []
    cost = 0.0
    calls = 0
    for turn in record.get("turns") or []:
        for call in turn.get("tool_calls") or []:
            result = call.get("result") or {}
            evidence.append(json.dumps(result, ensure_ascii=False))
            name = call.get("name")
            if name == "run_skill_script":
                payload = result.get("summary") or _json_stdout(result)
                ledger = payload.get("cost") or payload.get("aminer_cost") or {}
                cost += float(ledger.get("total_cost_cny") or 0)
                calls += int(ledger.get("total_calls") or 0)
            elif name == "verify_shortlist":
                ledger = _json_stdout(result).get("aminer_cost") or {}
                cost += float(ledger.get("total_cost_cny") or 0)
                calls += int(ledger.get("total_calls") or 0)
            elif name == "call_aminer_api" and not result.get("error"):
                cost += float(result.get("unit_price_cny") or 0)
                calls += 1
    evidence_text = "\n".join(evidence)
    ids = set(AMINER_LINK.findall(answer))
    missing_ids = sorted(identifier for identifier in ids if identifier not in evidence_text)
    stated = declared_total(answer)
    actual = round(cost, 2)
    errors = []
    if record.get("status") != "complete":
        errors.append("run is not complete")
    if not answer.strip():
        errors.append("final answer is empty")
    if missing_ids:
        errors.append("AMiner link IDs absent from tool results")
    if stated is not None and abs(stated - actual) > 0.10:
        errors.append("declared total differs from tool ledger by more than CNY 0.10")
    return {"run": record.get("run"), "status": "pass" if not errors else "fail",
            "errors": errors, "aminer_links": len(ids), "missing_ids": missing_ids,
            "ledger_cost_cny": actual, "ledger_calls": calls,
            "declared_total_cny": stated}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json", type=Path)
    args = parser.parse_args()
    result = audit(json.loads(args.run_json.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
