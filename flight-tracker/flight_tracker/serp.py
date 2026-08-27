"""SerpApi Google Flights client, plus a deterministic mock for offline testing."""

from __future__ import annotations

import hashlib
import os
import random
from typing import Any, Optional

import requests

ENDPOINT = "https://serpapi.com/search.json"

TYPE_ROUND_TRIP = 1
TYPE_ONE_WAY = 2


class SerpError(RuntimeError):
    pass


def _fmt_flight_numbers(flights: list[dict]) -> str:
    out = []
    for f in flights:
        num = f.get("flight_number")
        if num:
            out.append(num)
    return " / ".join(out)


def _summarize(best: dict, insights: dict | None) -> dict[str, Any]:
    flights = best.get("flights", []) or []
    airlines = []
    for f in flights:
        a = f.get("airline")
        if a and a not in airlines:
            airlines.append(a)
    layovers = best.get("layovers") or []
    result: dict[str, Any] = {
        "status": "ok",
        "price": best.get("price"),
        "airline": ", ".join(airlines) if airlines else None,
        "flight_numbers": _fmt_flight_numbers(flights) or None,
        "stops": len(layovers),
        "duration_min": best.get("total_duration"),
    }
    if insights:
        result["price_level"] = insights.get("price_level")
        rng = insights.get("typical_price_range") or []
        if len(rng) == 2:
            result["typical_low"], result["typical_high"] = rng[0], rng[1]
    return result


def search(
    query: dict[str, Any],
    *,
    api_key: str,
    currency: str = "USD",
    adults: int = 1,
    travel_class: int = 1,
    deep_search: bool = False,
    timeout: int = 60,
) -> dict[str, Any]:
    """Run one Google Flights search. Returns a normalized result dict."""
    qtype = TYPE_ONE_WAY if query.get("type") == "one_way" else TYPE_ROUND_TRIP
    params: dict[str, Any] = {
        "engine": "google_flights",
        "api_key": api_key,
        "departure_id": query["from"],
        "arrival_id": query["to"],
        "outbound_date": query["outbound"],
        "type": qtype,
        "currency": currency,
        "adults": adults,
        "travel_class": travel_class,
        "hl": "en",
        "gl": "us",
    }
    if qtype == TYPE_ROUND_TRIP:
        params["return_date"] = query["return"]
    if query.get("include_airlines"):
        params["include_airlines"] = ",".join(query["include_airlines"])
    if query.get("stops") is not None:
        params["stops"] = query["stops"]
    if deep_search:
        params["deep_search"] = "true"

    try:
        resp = requests.get(ENDPOINT, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return {"status": "error", "error": f"network: {exc}"}

    if resp.status_code != 200:
        return {
            "status": "error",
            "error": f"HTTP {resp.status_code}: {resp.text[:300]}",
        }

    data = resp.json()
    if data.get("error"):
        return {"status": "error", "error": str(data["error"])[:300]}

    candidates = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    priced = [c for c in candidates if isinstance(c.get("price"), (int, float))]
    if not priced:
        return {
            "status": "no_results",
            "error": "no priced itineraries returned (filters may be too narrow)",
        }

    cheapest = min(priced, key=lambda c: c["price"])
    out = _summarize(cheapest, data.get("price_insights"))
    out["currency"] = currency
    out["raw"] = {
        "price_insights": data.get("price_insights"),
        "chosen": {
            "price": cheapest.get("price"),
            "total_duration": cheapest.get("total_duration"),
            "type": cheapest.get("type"),
        },
        "n_candidates": len(priced),
    }
    return out


# --------------------------------------------------------------------- mock


def mock_search(query: dict[str, Any], *, day_index: int = 0, currency: str = "USD") -> dict[str, Any]:
    """Deterministic fake result so the pipeline can be tested without an API key.

    Prices follow a seeded random walk per query so day-over-day deltas look real.
    """
    qid = query["id"]
    h = int(hashlib.sha256(qid.encode()).hexdigest()[:8], 16)
    base = 520 + (h % 1100)
    if query.get("type") == "one_way":
        base *= 0.62

    drift = 0.0
    for d in range(day_index + 1):
        rr = random.Random(f"{qid}:{d}")
        drift += rr.uniform(-0.030, 0.030)
    price = round(max(120.0, base * (1 + drift)), 2)

    rr = random.Random(f"{qid}:{day_index}:meta")
    airlines = query.get("mock_airlines") or ["Mock Air", "Example Airways", "Testjet"]
    stops = rr.choice([0, 0, 1, 1, 2])
    dur = rr.randint(600, 1900)

    level = "low" if drift < -0.05 else ("high" if drift > 0.06 else "typical")
    return {
        "status": "ok",
        "price": price,
        "currency": currency,
        "airline": rr.choice(airlines),
        "flight_numbers": f"XX {rr.randint(100, 999)}",
        "stops": stops,
        "duration_min": dur,
        "price_level": level,
        "typical_low": round(base * 0.85, 2),
        "typical_high": round(base * 1.25, 2),
        "raw": {"mock": True, "day_index": day_index},
    }


def get_api_key(explicit: Optional[str] = None) -> str:
    key = explicit or os.environ.get("SERPAPI_KEY") or os.environ.get("SERPAPI_API_KEY")
    if not key:
        raise SerpError(
            "No SerpApi key found. Set SERPAPI_KEY in the environment "
            "(or pass --api-key). Run with --mock to test without one."
        )
    return key
