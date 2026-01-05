#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Builds a normalized events.json from Tibia.com's Event Calendar using Tibia.py (tibiapy).

Output JSON is designed to be consumed directly by a static HTML page (e.g., in Webnode via fetch()).

Notes (keep in code, not chat):
- Tibia.com may limit how far months can be fetched; requesting too far may return the current month instead.
- tibiapy uses lxml under the hood; on Linux you may need libxml/libxslt dev packages (GitHub Actions runners are OK).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests


# --- tibiapy imports with fallbacks (v6+ moved models under tibiapy.models in docs) ---
try:
    from tibiapy import EventSchedule  # type: ignore
except Exception:  # pragma: no cover
    from tibiapy.models import EventSchedule  # type: ignore


try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


STOCKHOLM_TZ = "Europe/Stockholm"


def now_stockholm() -> dt.datetime:
    if ZoneInfo is None:
        return dt.datetime.now()
    return dt.datetime.now(tz=ZoneInfo(STOCKHOLM_TZ))


def add_months(year: int, month: int, delta: int) -> Tuple[int, int]:
    # month: 1..12
    base = year * 12 + (month - 1)
    base += delta
    y = base // 12
    m = (base % 12) + 1
    return y, m


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    cur = dt.date(year, month, 1)
    return (nxt - cur).days


def clean_title(title: str) -> str:
    # Tibia.com shows '*' on boundary days; titles may contain leading '*'
    return title.lstrip("*").strip()


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    s = s.lower().strip()
    s = _slug_re.sub("-", s)
    return s.strip("-")


def is_css_color(value: Optional[str]) -> bool:
    if not value:
        return False
    v = value.strip()
    # Hex, rgb/rgba, or basic keyword (we'll accept keywords; browser will ignore invalid)
    if re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})", v):
        return True
    if v.lower().startswith(("rgb(", "rgba(", "hsl(", "hsla(")):
        return True
    # allow keywords like "green" etc
    if re.fullmatch(r"[a-zA-Z]+", v):
        return True
    return False


def categorize(title: str, description: str) -> str:
    t = title.lower()
    d = (description or "").lower()

    # Simple heuristics; tweak freely.
    if "double xp" in t or "xp" in t or "skill" in t or "rapid respawn" in t:
        return "boost"
    if "tibia anniversary" in t or "winterlight" in t or "halloween" in t or "christmas" in t or "new year" in t:
        return "seasonal"
    if "orc" in t or "devovorga" in t or "rise of" in t or "the first dragon" in t or "bewitched" in t:
        return "worldevent"
    if "full moon" in t:
        return "cycle"
    if "valentine" in t or "a piece of cake" in t or "spring into" in t or "colours of magic" in t:
        return "event"
    if "demon" in t or "exaltation" in t:
        return "event"
    if "last creep" in t:
        return "event"

    # fallback
    if "event" in d or "event" in t:
        return "event"
    return "misc"


@dataclass(frozen=True)
class Occurrence:
    title: str
    description: str
    color: Optional[str]
    start: dt.date
    end: dt.date

    @property
    def id(self) -> str:
        return f"{slugify(self.title)}-{self.start.isoformat()}"


def fetch_schedule(month: int, year: int, session: requests.Session, timeout: int, retries: int) -> "EventSchedule":
    url = EventSchedule.get_url(month=month, year=year)

    # Basic retry to handle transient failures / rate limiting.
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return EventSchedule.from_content(r.text)
        except Exception as e:
            last_exc = e
            # small backoff
            time.sleep(min(2.0 * attempt, 6.0))
    raise RuntimeError(f"Failed to fetch/parse event schedule for {year}-{month:02d}: {last_exc}")


def build_occurrences(
    schedules: Dict[Tuple[int, int], "EventSchedule"],
    months: List[Tuple[int, int]],
) -> Tuple[List[Occurrence], dt.date, dt.date]:
    # Map: title -> set of active dates
    active_dates: Dict[str, Set[dt.date]] = {}
    best_desc: Dict[str, str] = {}
    best_color: Dict[str, Optional[str]] = {}

    global_min: Optional[dt.date] = None
    global_max: Optional[dt.date] = None

    for (y, m) in months:
        sched = schedules[(y, m)]
        dim = days_in_month(y, m)

        for day in range(1, dim + 1):
            d = dt.date(y, m, day)
            day_events = sched.get_events_on(d)

            if not day_events:
                continue

            for e in day_events:
                t = clean_title(getattr(e, "title", "") or "")
                if not t:
                    continue

                desc = getattr(e, "description", "") or ""
                col = getattr(e, "color", None)

                active_dates.setdefault(t, set()).add(d)

                # prefer longest/most-informative description
                if desc and (len(desc) > len(best_desc.get(t, ""))):
                    best_desc[t] = desc

                # keep first valid color we see (if any)
                if t not in best_color:
                    best_color[t] = col if is_css_color(col) else None
                elif best_color[t] is None and is_css_color(col):
                    best_color[t] = col

                global_min = d if global_min is None else min(global_min, d)
                global_max = d if global_max is None else max(global_max, d)

    if global_min is None or global_max is None:
        # No events at all
        today = dt.date.today()
        return [], today, today

    occurrences: List[Occurrence] = []

    for title, dateset in active_dates.items():
        dates = sorted(dateset)
        if not dates:
            continue

        desc = best_desc.get(title, "")
        col = best_color.get(title)

        run_start = dates[0]
        prev = dates[0]

        for d in dates[1:]:
            if d == prev + dt.timedelta(days=1):
                prev = d
                continue
            # close run
            occurrences.append(Occurrence(title=title, description=desc, color=col, start=run_start, end=prev))
            run_start = d
            prev = d

        occurrences.append(Occurrence(title=title, description=desc, color=col, start=run_start, end=prev))

    occurrences.sort(key=lambda o: (o.start, o.title.lower()))
    return occurrences, global_min, global_max


def main() -> int:
    ap = argparse.ArgumentParser(description="Build TibiaSweden events.json from Tibia.com Event Calendar (via tibiapy).")
    ap.add_argument("--start-year", type=int, default=None)
    ap.add_argument("--start-month", type=int, default=None)
    ap.add_argument("--months-forward", type=int, default=10, help="How many months ahead to attempt (default: 10).")
    ap.add_argument("--months-back", type=int, default=1, help="How many months back to include (default: 1).")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--out", type=str, default="docs/events.json")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    now = now_stockholm()
    base_year = args.start_year or now.year
    base_month = args.start_month or now.month

    # Build month list (back .. forward)
    desired: List[Tuple[int, int]] = []
    for delta in range(-args.months_back, args.months_forward + 1):
        y, m = add_months(base_year, base_month, delta)
        desired.append((y, m))

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "TibiaSweden-EventBot/1.0 (+https://tibiasweden.se)",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    schedules: Dict[Tuple[int, int], "EventSchedule"] = {}
    months_fetched: List[Tuple[int, int]] = []

    last_seen: Optional[Tuple[int, int]] = None

    for (y, m) in desired:
        sched = fetch_schedule(m, y, session=session, timeout=args.timeout, retries=args.retries)

        actual = (int(getattr(sched, "year", 0)), int(getattr(sched, "month", 0)))

        # Tibia.com can return the current month if request is out-of-range.
        # If we detect repetition/mismatch, stop fetching further months in that direction.
        if actual != (y, m):
            if last_seen == actual:
                break
            # If mismatch occurs, we can still keep the returned month once, but avoid looping.
            if actual in schedules:
                break

        schedules[(actual[0], actual[1])] = sched
        months_fetched.append(actual)
        last_seen = actual

    # De-duplicate while preserving order
    seen = set()
    months_fetched_unique: List[Tuple[int, int]] = []
    for mm in months_fetched:
        if mm in seen:
            continue
        seen.add(mm)
        months_fetched_unique.append(mm)

    if not months_fetched_unique:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "generatedAt": now_stockholm().isoformat(timespec="seconds"),
                "timezone": STOCKHOLM_TZ,
                "source": "Tibia.com Event Calendar",
                "monthsFetched": [],
                "range": {"from": None, "to": None},
            },
            "events": [],
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None), encoding="utf-8")
        return 0

    occurrences, dmin, dmax = build_occurrences(schedules, months_fetched_unique)

    events_out = []
    for o in occurrences:
        cat = categorize(o.title, o.description)
        events_out.append(
            {
                "id": o.id,
                "title": o.title,
                "description": o.description,
                "startDate": o.start.isoformat(),
                "endDate": o.end.isoformat(),
                "color": o.color,
                "category": cat,
                "tibiaUrl": EventSchedule.get_url(month=o.start.month, year=o.start.year),
            }
        )

    payload = {
        "meta": {
            "generatedAt": now_stockholm().isoformat(timespec="seconds"),
            "timezone": STOCKHOLM_TZ,
            "source": "Tibia.com Event Calendar",
            "monthsFetched": [{"year": y, "month": m} for (y, m) in months_fetched_unique],
            "range": {"from": dmin.isoformat(), "to": dmax.isoformat()},
        },
        "events": events_out,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
