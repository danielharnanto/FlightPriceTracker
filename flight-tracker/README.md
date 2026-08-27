# Flight price tracker

Checks airline prices for three planned trips once a day, stores every reading,
and emails a report showing what moved.

## The trips it tracks

| Trip | Long-haul legs | Events driving the dates |
|---|---|---|
| **Fukuoka via Seoul** | EWR→ICN Dec 5 2026, ICN→EWR Dec 24 2026 (Air Premia preferred) | Fukuoka, Dec 19–20 2026 |
| **Asia multi-city** | JFK→HND out Feb 15–18 2027; home from Tokyo *or* Jakarta Mar 20–25 2027 | Aichi Feb 20–21 · Kaohsiung Mar 6–7 · Kuala Lumpur Mar 13 · Jakarta Mar 20 |
| **Europe multi-city** | JFK→FCO out Jun 2–3 2027, back Jun 13–14 2027 (Norse Atlantic preferred) | Milan Jun 5 · Paris Jun 7 · Amsterdam Jun 10 · Antwerp Jun 12 |

Intra-region hops (Milan→Paris→Amsterdam→Antwerp, Kaohsiung→KL→Jakarta) and
Japan rail are **not** tracked — long-haul only, by design. To add them, drop
more entries into the `queries` list of the relevant trip in `config.json`.

## Why it rotates

SerpApi's free tier is ~100 searches/month. Checking every date variant daily
would burn that in three days. Instead each trip has several date variants and
the tracker checks **one variant per trip per day**, rotating through the list —
3 searches/day, about **90/month**. Every variant gets re-checked every 2–7 days
depending on how many that trip has, and the report always shows the last known
price for the ones not checked today.

## Setup

```bash
pip install -r requirements.txt
export SERPAPI_KEY="your-key-here"
python -m flight_tracker.main
```

Try it with no API key and no network at all:

```bash
python -m flight_tracker.main --mock --backfill 21
open out/latest.html
```

`--backfill` seeds three weeks of synthetic history so you can see what the
trend columns look like once real data accumulates.

### Email

Set these environment variables, then pass `--email`:

| Variable | Example |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASS` | a Gmail **App Password**, not your account password |
| `REPORT_TO` | where the report goes |
| `REPORT_FROM` | usually same as `SMTP_USER` |

Gmail requires 2FA enabled, then an App Password from your Google Account
security settings. Any SMTP provider works — Fastmail, Resend, SES.

## Running it daily

Price history is the valuable part, so it needs to run somewhere that keeps
`data/prices.db` between runs.

**GitHub Actions** (included, easiest): push this folder to a private repo, add
`SERPAPI_KEY` and the SMTP values as repository secrets, and
`.github/workflows/daily.yml` runs at 07:10 ET and commits the database back.

**Your own machine** — cron:

```
10 7 * * * cd /path/to/flight-tracker && /usr/bin/python3 -m flight_tracker.main --email >> cron.log 2>&1
```

Note that a laptop that's asleep at 7am won't run it; `launchd` on macOS with
`StartCalendarInterval` will catch up on wake.

## Files

```
config.json              trips, date variants, airline filters, budget
flight_tracker/serp.py   SerpApi Google Flights client + offline mock
flight_tracker/db.py     SQLite history store
flight_tracker/report.py email-safe HTML + plain text report
flight_tracker/main.py   CLI, rotation, budget guard, SMTP send
data/prices.db           every reading ever taken  ← back this up
out/latest.html          most recent report
```

## Flags

| Flag | Does |
|---|---|
| `--mock` | fake prices, no API calls |
| `--backfill N` | with `--mock`, seed N days of history |
| `--email` | send over SMTP |
| `--date YYYY-MM-DD` | pretend it's another day (tests rotation) |
| `--force` | run even if the monthly budget is exceeded |

The tracker refuses to run if the month's searches would exceed
`monthly_search_budget` in `config.json`, so a runaway loop can't drain a paid
plan.

## Editing what's tracked

Each entry in a trip's `queries` list is one search:

```json
{
  "id": "EUR_rt_norse_0602_0613",
  "label": "Norse · out Jun 2 · back Jun 13",
  "type": "round_trip",
  "from": "JFK", "to": "FCO",
  "outbound": "2027-06-02", "return": "2027-06-13",
  "include_airlines": ["N0"]
}
```

`type` is `round_trip` or `one_way` (one-way omits `return`).
`include_airlines` takes IATA codes — `YP` Air Premia, `N0` Norse Atlantic.
Keep `id` stable: it's the key price history is filed under, so renaming one
starts its history over.

Adding queries increases searches per month. The formula is
`trips × searches_per_trip_per_day × 30`.
