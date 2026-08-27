"""Build the daily report (email-safe HTML + plain text) from stored history."""

from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any, Optional

# Email clients are hostile to modern CSS, so: tables, inline styles, no flex/grid.
INK = "#16181d"
MUTED = "#6b7280"
LINE = "#e5e7eb"
BG = "#f6f7f9"
CARD = "#ffffff"
DOWN = "#0f7a3d"
UP = "#b42318"
ACCENT = "#1f4ed8"


def money(v: Optional[float], currency: str = "USD") -> str:
    if v is None:
        return "—"
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{sym}{v:,.0f}" if sym else f"{v:,.0f} {currency}"


def dur(minutes: Optional[int]) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def stops_label(n: Optional[int]) -> str:
    if n is None:
        return "—"
    return "nonstop" if n == 0 else (f"{n} stop" if n == 1 else f"{n} stops")


def delta_bits(cur: Optional[float], prev: Optional[float]) -> tuple[str, str, str]:
    """Return (arrow, text, color)."""
    if cur is None or prev is None:
        return ("", "no prior reading", MUTED)
    diff = cur - prev
    if abs(diff) < 0.5:
        return ("→", "unchanged", MUTED)
    pct = (diff / prev * 100) if prev else 0
    if diff < 0:
        return ("▼", f"−{money(abs(diff))} ({abs(pct):.1f}%)", DOWN)
    return ("▲", f"+{money(diff)} ({pct:.1f}%)", UP)


def _sparkline(prices: list[float]) -> str:
    """Tiny unicode sparkline — renders in most mail clients, degrades gracefully."""
    if len(prices) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(prices), max(prices)
    if hi - lo < 0.01:
        return blocks[0] * len(prices)
    return "".join(blocks[int((p - lo) / (hi - lo) * (len(blocks) - 1))] for p in prices)


class ReportBuilder:
    def __init__(self, store, config: dict[str, Any], today: date, checked_ids: set[str]):
        self.store = store
        self.config = config
        self.today = today
        self.checked = checked_ids
        self.currency = config.get("currency", "USD")

    # ------------------------------------------------------------- gathering

    def query_view(self, trip: dict, q: dict) -> dict[str, Any]:
        latest = self.store.latest(q["id"])
        prev = self.store.latest(q["id"], offset=1)
        stats = self.store.stats(q["id"])
        hist = self.store.history(q["id"], limit=14)
        cur_price = latest["price"] if latest else None
        prev_price = prev["price"] if prev else None
        arrow, dtext, dcolor = delta_bits(cur_price, prev_price)
        last_any = self.store.last_row(q["id"])
        # A route the airline hasn't published yet looks identical to a broken
        # query unless we say so explicitly.
        awaiting = bool(
            last_any is not None
            and last_any["status"] == "no_results"
            and cur_price is None
        )
        return {
            "awaiting_schedule": awaiting,
            "last_status": last_any["status"] if last_any else None,
            "q": q,
            "checked_today": q["id"] in self.checked,
            "latest": latest,
            "price": cur_price,
            "prev_price": prev_price,
            "arrow": arrow,
            "delta_text": dtext,
            "delta_color": dcolor,
            "stats": stats,
            "spark": _sparkline([h["price"] for h in hist]),
            "n_readings": stats["n"],
            "is_all_time_low": (
                cur_price is not None
                and stats["n"] > 1
                and stats["low"] is not None
                and cur_price <= stats["low"] + 0.01
            ),
        }

    def trip_view(self, trip: dict) -> dict[str, Any]:
        views = [self.query_view(trip, q) for q in trip["queries"]]
        oj = self._open_jaw(trip)

        # The headline number must be a COMPLETE trip. On an open-jaw trip the
        # one-way legs are half a journey, so comparing them against round
        # trips would headline a price that doesn't get you home.
        if oj:
            headline = self._open_jaw_headline(trip, views, oj)
        else:
            priced = [v for v in views if v["price"] is not None]
            headline = min(priced, key=lambda v: v["price"]) if priced else None

        return {"trip": trip, "views": views, "cheapest": headline, "open_jaw": oj}

    def _open_jaw_headline(self, trip: dict, views: list[dict], oj: dict) -> Optional[dict]:
        """Cheaper of (best round trip) and (best one-way out + one-way home)."""
        options = []
        if oj["rt_total"] is not None:
            rt_view = next(
                (v for v in views if v["q"]["label"] == oj["rt_label"]), None
            )
            options.append(("round_trip", oj["rt_total"], rt_view))
        if oj["combo_total"] is not None:
            options.append(("combo", oj["combo_total"], None))
        if not options:
            return None

        kind, total, src = min(options, key=lambda o: o[1])
        if kind == "round_trip" and src:
            return src

        # Synthetic view for the two-one-way combination; delta is not
        # meaningful across two independently-sampled legs, so omit it.
        return {
            "price": total,
            "prev_price": None,
            "arrow": "",
            "delta_text": "two one-ways",
            "delta_color": MUTED,
            "checked_today": False,
            "latest": None,
            "is_all_time_low": False,
        }

    def _open_jaw(self, trip: dict) -> Optional[dict[str, Any]]:
        """For the Asia trip: is round trip or (one-way out + one-way home) cheaper?"""
        spec = trip.get("compare_open_jaw")
        if not spec:
            return None

        def best_of(ids):
            rows = [(i, self.store.latest(i)) for i in ids]
            rows = [(i, r) for i, r in rows if r and r["price"] is not None]
            if not rows:
                return None
            return min(rows, key=lambda t: t[1]["price"])

        rt = best_of(spec.get("round_trip_ids", []))
        out = best_of(spec.get("outbound_ids", []))
        back = best_of(spec.get("return_ids", []))
        if not rt and not (out and back):
            return None

        combo_total = (out[1]["price"] + back[1]["price"]) if (out and back) else None
        rt_total = rt[1]["price"] if rt else None

        winner = None
        if combo_total is not None and rt_total is not None:
            winner = "combo" if combo_total < rt_total else "round_trip"
        elif combo_total is not None:
            winner = "combo"
        elif rt_total is not None:
            winner = "round_trip"

        def label_for(pair):
            if not pair:
                return None
            qid = pair[0]
            for q in trip["queries"]:
                if q["id"] == qid:
                    return q["label"]
            return qid

        return {
            "rt_total": rt_total,
            "rt_label": label_for(rt),
            "combo_total": combo_total,
            "out_price": out[1]["price"] if out else None,
            "out_label": label_for(out),
            "back_price": back[1]["price"] if back else None,
            "back_label": label_for(back),
            "winner": winner,
            "savings": (
                abs(combo_total - rt_total)
                if (combo_total is not None and rt_total is not None)
                else None
            ),
        }

    # ---------------------------------------------------------------- render

    def build(self, *, budget_used: int, budget_total: int, errors: list[str], next_up: list[str]) -> tuple[str, str, str]:
        trips = [self.trip_view(t) for t in self.config["trips"]]
        subject = self._subject(trips)
        return subject, self._html(trips, budget_used, budget_total, errors, next_up), self._text(trips, budget_used, budget_total, errors)

    def _subject(self, trips: list[dict]) -> str:
        movers = []
        for tv in trips:
            for v in tv["views"]:
                if v["checked_today"] and v["price"] is not None and v["prev_price"] is not None:
                    movers.append((v["price"] - v["prev_price"], tv, v))
        stamp = self.today.strftime("%b %-d") if hasattr(self.today, "strftime") else str(self.today)
        if not movers:
            return f"Flight prices — {stamp}"
        movers.sort(key=lambda m: m[0])
        biggest = movers[0] if abs(movers[0][0]) >= abs(movers[-1][0]) else movers[-1]
        diff, tv, v = biggest
        if abs(diff) < 1:
            return f"Flight prices — {stamp} · little movement"
        direction = "down" if diff < 0 else "up"
        return (
            f"Flight prices — {stamp} · {tv['trip']['name'].split('—')[0].strip()} "
            f"{direction} {money(abs(diff), self.currency)}"
        )

    # -- HTML -------------------------------------------------------------

    def _html(self, trips, budget_used, budget_total, errors, next_up) -> str:
        e = html.escape
        stamp = self.today.strftime("%A, %B %-d, %Y")
        parts = [f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Price Report</title></head>
<body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{INK};">

<tr><td style="padding:0 4px 18px;">
  <div style="font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:{MUTED};font-weight:600;">Daily flight tracker</div>
  <div style="font-size:26px;font-weight:700;line-height:1.25;margin-top:6px;">{e(stamp)}</div>
</td></tr>"""]

        # summary row
        parts.append(f'<tr><td style="padding:0 0 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
        for tv in trips:
            c = tv["cheapest"]
            name = tv["trip"]["name"].split("—")[0].strip()
            if c:
                price = money(c["price"], self.currency)
                sub = f'{c["arrow"]} {c["delta_text"]}' if c["arrow"] else "first reading"
                color = c["delta_color"]
            else:
                price, sub, color = "—", "no data yet", MUTED
            parts.append(f"""<tr><td style="padding:0 0 8px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-radius:10px;">
<tr>
<td style="padding:14px 16px;font-size:14px;font-weight:600;">{e(name)}</td>
<td align="right" style="padding:14px 16px;white-space:nowrap;">
  <span style="font-size:20px;font-weight:700;">{price}</span>
  <span style="font-size:12px;color:{color};display:block;margin-top:2px;">{e(sub)}</span>
</td></tr></table></td></tr>""")
        parts.append("</table></td></tr>")

        # trips
        for tv in trips:
            trip = tv["trip"]
            parts.append(f"""<tr><td style="padding:20px 0 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CARD};border:1px solid {LINE};border-radius:12px;">
<tr><td style="padding:18px 18px 6px;">
  <div style="font-size:17px;font-weight:700;">{e(trip["name"])}</div>
  <div style="font-size:12.5px;color:{MUTED};line-height:1.55;margin-top:5px;">{e(trip.get("events",""))}</div>
</td></tr>""")

            todays = [v for v in tv["views"] if v["checked_today"]]
            for v in todays:
                lt = v["latest"]
                badge = ""
                if v["is_all_time_low"]:
                    badge = f'<span style="background:#e7f6ec;color:{DOWN};font-size:11px;font-weight:700;padding:2px 7px;border-radius:99px;margin-left:8px;">lowest seen</span>'
                meta = []
                if lt:
                    if lt["airline"]:
                        meta.append(e(str(lt["airline"])))
                    meta.append(stops_label(lt["stops"]))
                    meta.append(dur(lt["duration_min"]))
                    if lt["price_level"]:
                        meta.append(f'Google: {e(str(lt["price_level"]))}')
                parts.append(f"""<tr><td style="padding:6px 18px 4px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;border:1px solid {LINE};border-radius:9px;">
<tr><td style="padding:14px 15px;">
  <div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:{ACCENT};font-weight:700;">Checked today</div>
  <div style="font-size:14.5px;font-weight:600;margin-top:5px;">{e(v["q"]["label"])}{badge}</div>
  <div style="margin-top:9px;">
    <span style="font-size:25px;font-weight:700;">{money(v["price"], self.currency)}</span>
    <span style="font-size:13px;color:{v["delta_color"]};font-weight:600;margin-left:9px;">{v["arrow"]} {e(v["delta_text"])}</span>
  </div>
  <div style="font-size:12.5px;color:{MUTED};margin-top:7px;">{e(" · ".join(meta)) if meta else ""}</div>
</td></tr></table></td></tr>""")

            # variant table
            parts.append(f"""<tr><td style="padding:12px 18px 4px;">
<div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:{MUTED};font-weight:700;padding-bottom:7px;">All tracked variants</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:12.5px;border-collapse:collapse;">
<tr style="color:{MUTED};">
  <td style="padding:6px 6px 6px 0;border-bottom:1px solid {LINE};">Option</td>
  <td align="right" style="padding:6px;border-bottom:1px solid {LINE};">Latest</td>
  <td align="right" style="padding:6px;border-bottom:1px solid {LINE};">Change</td>
  <td align="right" style="padding:6px;border-bottom:1px solid {LINE};">Low / High</td>
  <td align="right" style="padding:6px 0 6px 6px;border-bottom:1px solid {LINE};">Seen</td>
</tr>""")
            for v in tv["views"]:
                st = v["stats"]
                mark = "●" if v["checked_today"] else "○"
                lohi = (
                    f'{money(st["low"], self.currency)} / {money(st["high"], self.currency)}'
                    if st["n"] else "—"
                )
                when = v["latest"]["observed_date"] if v["latest"] else "never"
                if v["awaiting_schedule"]:
                    lohi = "not yet in schedules"
                    when = v["last_status"] and self.store.last_row(v["q"]["id"])["observed_date"] or when
                spark = f'<div style="color:{MUTED};font-size:11px;letter-spacing:1px;">{v["spark"]}</div>' if v["spark"] else ""
                parts.append(f"""<tr>
  <td style="padding:8px 6px 8px 0;border-bottom:1px solid #f1f2f4;">
    <span style="color:{ACCENT if v["checked_today"] else LINE};">{mark}</span> {e(v["q"]["label"])}{spark}</td>
  <td align="right" style="padding:8px 6px;border-bottom:1px solid #f1f2f4;font-weight:600;white-space:nowrap;">{money(v["price"], self.currency)}</td>
  <td align="right" style="padding:8px 6px;border-bottom:1px solid #f1f2f4;color:{v["delta_color"]};white-space:nowrap;">{v["arrow"]} {e(v["delta_text"]) if v["prev_price"] else "—"}</td>
  <td align="right" style="padding:8px 6px;border-bottom:1px solid #f1f2f4;color:{MUTED};white-space:nowrap;">{lohi}</td>
  <td align="right" style="padding:8px 0 8px 6px;border-bottom:1px solid #f1f2f4;color:{MUTED};white-space:nowrap;">{e(str(when))}</td>
</tr>""")
            parts.append("</table></td></tr>")

            # open-jaw comparison
            oj = tv["open_jaw"]
            if oj:
                if oj["winner"] == "combo":
                    verdict = f'Two one-ways win by {money(oj["savings"], self.currency)}'
                    vcolor = DOWN
                elif oj["winner"] == "round_trip":
                    verdict = f'Round trip wins by {money(oj["savings"], self.currency)}'
                    vcolor = DOWN
                else:
                    verdict = "Not enough data yet"
                    vcolor = MUTED
                combo_detail = (
                    f'{money(oj["out_price"], self.currency)} out + {money(oj["back_price"], self.currency)} home'
                    if oj["combo_total"] is not None else "waiting on readings"
                )
                parts.append(f"""<tr><td style="padding:10px 18px 18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fbfaf7;border:1px solid #eee7d8;border-radius:9px;">
<tr><td style="padding:14px 15px;">
  <div style="font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:#8a6d3b;font-weight:700;">Round trip vs. open jaw</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:9px;font-size:13px;">
    <tr><td style="padding:3px 0;">Round trip via Tokyo<div style="font-size:11.5px;color:{MUTED};">{e(str(oj["rt_label"] or "—"))}</div></td>
        <td align="right" style="padding:3px 0;font-weight:700;">{money(oj["rt_total"], self.currency)}</td></tr>
    <tr><td style="padding:3px 0;">Out to Tokyo + home from Jakarta<div style="font-size:11.5px;color:{MUTED};">{e(combo_detail)}</div></td>
        <td align="right" style="padding:3px 0;font-weight:700;">{money(oj["combo_total"], self.currency)}</td></tr>
  </table>
  <div style="margin-top:10px;font-size:13px;font-weight:700;color:{vcolor};">{e(verdict)}</div>
</td></tr></table></td></tr>""")
            else:
                parts.append('<tr><td style="padding:0 0 10px;"></td></tr>')

            parts.append("</table></td></tr>")

        # errors
        if errors:
            items = "".join(f'<li style="margin-bottom:4px;">{e(x)}</li>' for x in errors)
            parts.append(f"""<tr><td style="padding:18px 0 0;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#fef6f5;border:1px solid #f6d9d5;border-radius:10px;">
<tr><td style="padding:14px 16px;font-size:13px;color:{UP};">
<strong>Searches that failed today</strong>
<ul style="margin:8px 0 0;padding-left:18px;">{items}</ul>
</td></tr></table></td></tr>""")

        nxt = ", ".join(next_up) if next_up else "—"
        parts.append(f"""<tr><td style="padding:20px 4px 8px;font-size:11.5px;color:{MUTED};line-height:1.7;">
  API searches used this month: <strong>{budget_used}</strong> of {budget_total}.<br>
  Next in rotation: {e(nxt)}<br>
  Prices are the cheapest itinerary matching each filter at the time of the check, for {self.config.get("adults",1)} adult(s), and can change before you book.
</td></tr>

</table></td></tr></table></body></html>""")
        return "".join(parts)

    # -- plain text --------------------------------------------------------

    def _text(self, trips, budget_used, budget_total, errors) -> str:
        L: list[str] = []
        L.append(f"FLIGHT PRICE REPORT — {self.today}")
        L.append("=" * 58)
        for tv in trips:
            L.append("")
            L.append(tv["trip"]["name"])
            L.append("-" * 58)
            for v in tv["views"]:
                mark = "*" if v["checked_today"] else " "
                st = v["stats"]
                line = f'{mark} {v["q"]["label"]}: {money(v["price"], self.currency)}'
                if v["prev_price"]:
                    line += f' {v["arrow"]} {v["delta_text"]}'
                if st["n"]:
                    line += f' | low {money(st["low"], self.currency)} high {money(st["high"], self.currency)}'
                L.append(line)
            oj = tv["open_jaw"]
            if oj:
                L.append(f'  Round trip {money(oj["rt_total"], self.currency)} vs '
                         f'open jaw {money(oj["combo_total"], self.currency)}'
                         f' -> {oj["winner"] or "insufficient data"}')
        if errors:
            L.append("")
            L.append("FAILED SEARCHES:")
            L.extend(f"  - {x}" for x in errors)
        L.append("")
        L.append(f"API searches this month: {budget_used}/{budget_total}")
        L.append("(* = checked today)")
        return "\n".join(L)
