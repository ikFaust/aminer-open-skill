#!/usr/bin/env python3
"""AMiner Open Platform client with retries and per-call cost accounting."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


BASE_URL = "https://datacenter.aminer.cn/gateway/open_platform"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

API_SPEC: dict[str, tuple[str, str, float]] = {
    "person_search": ("POST", "/api/person/search", 0.0),
    "paper_info": ("POST", "/api/paper/info", 0.0),
    "org_search": ("POST", "/api/organization/search", 0.0),
    "paper_search_pro": ("GET", "/api/paper/search/pro", 0.01),
    "paper_qa_search": ("POST", "/api/paper/qa/search", 0.05),
    "paper_detail": ("GET", "/api/paper/detail", 0.01),
    "org_detail": ("POST", "/api/organization/detail", 0.01),
    "person_detail": ("GET", "/api/person/detail", 1.0),
    "person_figure": ("GET", "/api/person/figure", 0.5),
    "person_paper_relation": ("GET", "/api/person/paper/relation", 1.5),
    "org_person_relation": ("GET", "/api/organization/person/relation", 0.5),
}


@dataclass
class CostLedger:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, api: str) -> None:
        self.calls.append({"api": api, "unit_price_cny": API_SPEC[api][2]})

    def summary(self) -> dict[str, Any]:
        breakdown: dict[str, dict[str, Any]] = {}
        for item in self.calls:
            api = item["api"]
            row = breakdown.setdefault(
                api, {"calls": 0, "unit_price_cny": item["unit_price_cny"], "cost_cny": 0.0}
            )
            row["calls"] += 1
            row["cost_cny"] = round(row["calls"] * row["unit_price_cny"], 2)
        return {
            "total_calls": len(self.calls),
            "total_cost_cny": round(sum(x["unit_price_cny"] for x in self.calls), 2),
            "breakdown": breakdown,
        }


class AMinerClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: float = 30,
        max_retries: int = 3,
        base_url: str = BASE_URL,
    ) -> None:
        if not token:
            raise ValueError("AMINER_API_KEY is missing")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.cost = CostLedger()

    def call(self, api: str, params: dict[str, Any]) -> Any:
        if api not in API_SPEC:
            raise ValueError(f"Unsupported API: {api}")
        method, path, _ = API_SPEC[api]
        self.cost.add(api)
        headers = {
            "Authorization": self.token,
            "X-Platform": "openclaw",
            "Content-Type": "application/json;charset=utf-8",
        }
        url = self.base_url + path
        body = None
        if method == "GET":
            query = urllib.parse.urlencode(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (list, dict))
                    else value
                    for key, value in params.items()
                    if value is not None
                }
            )
            if query:
                url += "?" + query
        else:
            body = json.dumps(params, ensure_ascii=False).encode("utf-8")

        last_error: dict[str, Any] | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return payload
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    detail: Any = json.loads(raw)
                except json.JSONDecodeError:
                    detail = raw
                last_error = {
                    "code": exc.code,
                    "success": False,
                    "msg": str(exc.reason),
                    "error": detail,
                }
                if exc.code not in RETRYABLE_STATUS:
                    return last_error
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = {"code": -1, "success": False, "msg": str(exc)}
            if attempt + 1 < self.max_retries:
                time.sleep((2**attempt) + random.uniform(0, 0.2))
        return last_error or {"code": -1, "success": False, "msg": "request failed"}


def unwrap(payload: Any) -> list[dict[str, Any]]:
    """Return record dictionaries from the standard AMiner envelope."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, choices=sorted(API_SPEC))
    parser.add_argument("--params", default="{}", help="JSON object")
    parser.add_argument("--timeout", type=float, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --params JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise SystemExit("--params must decode to a JSON object")
    try:
        client = AMinerClient(os.getenv("AMINER_API_KEY", ""), timeout=args.timeout)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    result = client.call(args.api, params)
    json.dump(
        {"api": args.api, "cost": client.cost.summary(), "result": result},
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
