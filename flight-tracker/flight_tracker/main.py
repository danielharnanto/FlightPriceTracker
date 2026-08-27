"""CLI entry point: run the daily check, store it, build and optionally email the report."""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from . import db as dbmod
from . import serp
from .report import ReportBuilder

EPOCH = date(2026, 1, 1)


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def day_index(d: date) -> int:
    return (d - EPOCH).days


def pick_queries(config: dict, d: date) -> list[tuple[dict, dict]]:
    """One query per trip per day, rotating through each trip's variant list.

    Different trips use offset rotations so a single day never checks the same
    slot in every list (keeps coverage even if runs are missed).
    """
    per_trip = int(config.get("searches_per_trip_per_day", 1))
    di = day_index(d)
    picks: list[tuple[dict, dict]] = []
    for offset, trip in enumerate(config["trips"]):
        qs = trip["queries"]
        if not qs:
            continue
        for k in range(min(per_trip, len(qs))):
            idx = (di * per_trip + k + offset) % len(qs)
            picks.append((trip, qs[idx]))
    return picks


def preview_next(config: dict, d: date, days: int = 3) -> list[str]:
    out = []
    for n in range(1, days + 1):
        nd = d + timedelta(days=n)
        labels = [q["label"] for _, q in pick_queries(config, nd)]
        out.append(f'{nd.strftime("%b %-d")}: ' + "; ".join(labels))
    return out


def send_email(subject: str, html_body: str, text_body: str) -> str:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to_addr = os.environ.get("REPORT_TO")
    from_addr = os.environ.get("REPORT_FROM", user or "")

    missing = [k for k, v in {
        "SMTP_HOST": host, "SMTP_USER": user, "SMTP_PASS": password, "REPORT_TO": to_addr
    }.items() if not v]
    if missing:
        return f"email skipped — missing env: {', '.join(missing)}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=45) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        return f"email sent to {to_addr}"
    except Exception as exc:  # noqa: BLE001
        return f"email FAILED: {exc}"


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = dbmod.Store(args.db)
    today = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- optional mock backfill so trends exist immediately when testing
    if args.backfill and args.mock:
        print(f"Backfilling {args.backfill} days of mock history...")
        for n in range(args.backfill, 0, -1):
            d = today - timedelta(days=n)
            for trip, q in pick_queries(config, d):
                res = serp.mock_search(q, day_index=day_index(d), currency=config.get("currency", "USD"))
                store.record(
                    trip_id=trip["id"], query_id=q["id"], result=res,
                    observed_at=f"{d.isoformat()}T12:00:00+00:00", observed_date=d.isoformat(),
                )

    picks = pick_queries(config, today)
    budget_total = int(config.get("monthly_search_budget", 100))
    already = store.searches_this_month()

    run_id = store.start_run(mock=args.mock)
    errors: list[str] = []
    ok = fail = used = 0

    api_key = None
    if not args.mock:
        if already + len(picks) > budget_total:
            print(
                f"WARNING: {already} searches already used this month; "
                f"{len(picks)} more would exceed the budget of {budget_total}.",
                file=sys.stderr,
            )
            if not args.force:
                store.finish_run(run_id, searches=0, ok=0, fail=0, notes="aborted: budget")
                print("Aborting. Re-run with --force to override.", file=sys.stderr)
                return 2
        api_key = serp.get_api_key(args.api_key)

    checked_ids: set[str] = set()
    for trip, q in picks:
        label = f'{trip["id"]}/{q["id"]}'
        print(f"Checking {label} — {q['label']}")
        if args.mock:
            res = serp.mock_search(q, day_index=day_index(today), currency=config.get("currency", "USD"))
        else:
            res = serp.search(
                q,
                api_key=api_key,
                currency=config.get("currency", "USD"),
                adults=int(config.get("adults", 1)),
                travel_class=int(config.get("travel_class", 1)),
                deep_search=bool(config.get("deep_search", False)),
            )
            used += 1
        store.record(trip_id=trip["id"], query_id=q["id"], result=res)
        checked_ids.add(q["id"])
        if res.get("status") == "ok":
            ok += 1
            print(f"   -> {res.get('price')} {res.get('currency')} ({res.get('airline')})")
        else:
            fail += 1
            msg = f'{q["label"]}: {res.get("error") or res.get("status")}'
            errors.append(msg)
            print(f"   -> FAILED: {msg}", file=sys.stderr)

    store.finish_run(run_id, searches=used, ok=ok, fail=fail)

    builder = ReportBuilder(store, config, today, checked_ids)
    subject, html_body, text_body = builder.build(
        budget_used=store.searches_this_month(),
        budget_total=budget_total,
        errors=errors,
        next_up=preview_next(config, today, days=2),
    )

    dated = outdir / f"report-{today.isoformat()}.html"
    latest = outdir / "latest.html"
    dated.write_text(html_body, encoding="utf-8")
    latest.write_text(html_body, encoding="utf-8")
    (outdir / "latest.txt").write_text(text_body, encoding="utf-8")

    print(f"\nSubject: {subject}")
    print(f"Report written to {dated}")

    if args.email:
        print(send_email(subject, html_body, text_body))

    store.close()
    return 0 if fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flight-tracker", description="Daily airline price tracker")
    p.add_argument("--config", default="config.json")
    p.add_argument("--db", default="data/prices.db")
    p.add_argument("--out", default="out")
    p.add_argument("--api-key", default=None, help="SerpApi key (prefer SERPAPI_KEY env var)")
    p.add_argument("--mock", action="store_true", help="use fake prices; no API calls")
    p.add_argument("--backfill", type=int, default=0, help="with --mock, seed N days of history")
    p.add_argument("--email", action="store_true", help="send the report over SMTP")
    p.add_argument("--date", default=None, help="override today's date (YYYY-MM-DD)")
    p.add_argument("--force", action="store_true", help="run even if over monthly budget")
    args = p.parse_args(argv)
    try:
        return run(args)
    except serp.SerpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
