#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Builds a normalized events.json from Tibia.com's Event Calendar using Tibia.py (tibiapy).

Output JSON is designed to be consumed directly by a static HTML page (e.g., in Webnode via fetch()).

Notes (keep in code, not chat):
- Tibia.com may limit how far months can be fetched; requesting too far may return the current month instead.
- tibia.py uses lxml under the hood; on Linux you may need libxml/libxslt dev packages (GitHub Actions runners are OK).
- tibia.py v6+ removed get_url class methods from models; URL helpers live in tibiapy.urls.
- tibia.py v6+ parsing is provided via tibiapy.parsers.* (e.g., EventScheduleParser).
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


# Optional helpers for newer tibia.py versions (v6+): URL functions moved to tibiapy.urls,
# and parsing is available via tibiapy.parsers.*. See upstream changelog/docs.
try:  # pragma: no cover
    from tibiapy import urls as tibi_urls  # type: ignore
except Exception:  # pragma: no cover
    tibi_urls = None  # type: ignore

try:  # pragma: no cover
    from tibiapy.parsers import EventScheduleParser  # type: ignore
except Exception:  # pragma: no cover
    EventScheduleParser = None  # type: ignore


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


def normalize_color(color: Optional[str]) -> Optional[str]:
    if not color:
        return None
    c = color.strip()
    # Tibia's calendar uses things like "#xxxxxx" or named-ish; keep as-is
    return c


def safe_iso_date(d: dt.date) -> str:
    return d.isoformat()


def daterange_inclusive(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    cur = start
    while cur <= end:
        yield cur
        cur += dt.timedelta(days=1)


def clamp_end(start: dt.date, end: dt.date) -> dt.date:
    # ensure end >= start
    if end < start:
        return start
    return end


def category_hint(title: str, description: str) -> str:
    """
    Very lightweight categorization.
    Keep it simple: the front-end can group by date anyway.
    """
    t = title.lower()
    d = description.lower()

    if "double" in t or "xp" in t or "skill" in t:
        return "boost"
    if "rapid" in t or "respawn" in t:
        return "boost"
    if "halloween" in t or "christmas" in t or "new year" in t or "valentine" in t:
        return "seasonal"
    if "orcsober" in t:
        return "seasonal"
    if "full moon" in t:
        return "moon"
    if "rise of" in t:
        return "event"
    if "bewitched" in t:
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


def event_schedule_url(month: int, year: int) -> str:
    """Return the Tibia.com Event Schedule URL for a given month/year across tibia.py versions."""
    # tibia.py v6+: get_url removed from models; use urls.get_event_schedule_url or the model's .url property.
    if tibi_urls is not None and hasattr(tibi_urls, "get_event_schedule_url"):
        return tibi_urls.get_event_schedule_url(month=month, year=year)  # type: ignore[attr-defined]
    if hasattr(EventSchedule, "get_url"):
        return EventSchedule.get_url(month=month, year=year)  # type: ignore[attr-defined]
    return EventSchedule(month=month, year=year).url


def parse_event_schedule(html: str) -> "EventSchedule":
    """Parse EventSchedule HTML into an EventSchedule instance across tibia.py versions."""
    if EventScheduleParser is not None and hasattr(EventScheduleParser, "from_content"):
        parsed = EventScheduleParser.from_content(html)  # type: ignore[attr-defined]
        if parsed is None:
            raise RuntimeError("EventScheduleParser returned None (invalid content?)")
        return parsed
    if hasattr(EventSchedule, "from_content"):
        return EventSchedule.from_content(html)  # type: ignore[attr-defined]
    raise RuntimeError("No supported EventSchedule parser found in this tibia.py version")


def fetch_schedule(month: int, year: int, session: requests.Session, timeout: int, retries: int) -> "EventSchedule":
    url = event_schedule_url(month=month, year=year)

    # Basic retry to handle transient failures / rate limiting.
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
            r.raise_for_status()
            return parse_event_schedule(r.text)
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

            # Get events for the specific date.
            # tibia.py schedules generally include previous/next month spillovers,
            # so we only query within the "real" month range we built above.
            # If the API provides a helper, great; otherwise just filter.
            events = []
            if hasattr(sched, "get_events_on"):
                try:
                    events = list(sched.get_events_on(d))  # type: ignore[attr-defined]
                except Exception:
                    events = []
            if not events:
                # Fallback: brute filter. Events are typically EventEntry with start_date/end_date.
                if hasattr(sched, "events"):
                    for e in sched.events:  # type: ignore[attr-defined]
                        sd = getattr(e, "start_date", None)
                        ed = getattr(e, "end_date", None)
                        if isinstance(sd, dt.date) and isinstance(ed, dt.date):
                            if sd <= d <= ed:
                                events.append(e)

            if not events:
                continue

            for e in events:
                title = clean_title(getattr(e, "title", "") or "")
                if not title:
                    continue
                desc = (getattr(e, "description", "") or "").strip()
                color = normalize_color(getattr(e, "color", None))

                active_dates.setdefault(title, set()).add(d)

                # Keep the richest description we saw.
                if title not in best_desc or (desc and len(desc) > len(best_desc[title])):
                    best_desc[title] = desc

                # Prefer a non-null color if present.
                if title not in best_color or (best_color[title] is None and color is not None):
                    best_color[title] = color

                if global_min is None or d < global_min:
                    global_min = d
                if global_max is None or d > global_max:
                    global_max = d

    if global_min is None or global_max is None:
        # No events at all
        today = now_stockholm().date()
        return [], today, today

    occurrences: List[Occurrence] = []
    for title, dates in active_dates.items():
        sorted_dates = sorted(dates)
        start = sorted_dates[0]
        end = sorted_dates[-1]

        # NOTE: Some events mark boundary days with '*' on Tibia.com (server save day).
        # We keep inclusive ranges since UI can display markers if desired.
        occurrences.append(
            Occurrence(
                title=title,
                description=best_desc.get(title, ""),
                color=best_color.get(title),
                start=start,
                end=clamp_end(start, end),
            )
        )

    # sort by start date then title
    occurrences.sort(key=lambda o: (o.start, o.end, o.title.lower()))
    return occurrences, global_min, global_max


def compute_happening_and_upcoming(
    occurrences: List[Occurrence],
    today: dt.date,
    upcoming_days: int,
) -> Tuple[List[Occurrence], List[Occurrence]]:
    happening: List[Occurrence] = []
    upcoming: List[Occurrence] = []

    window_end = today + dt.timedelta(days=upcoming_days)

    for o in occurrences:
        if o.start <= today <= o.end:
            happening.append(o)
        elif today < o.start <= window_end:
            upcoming.append(o)

    happening.sort(key=lambda o: (o.end, o.start, o.title.lower()))
    upcoming.sort(key=lambda o: (o.start, o.end, o.title.lower()))
    return happening, upcoming


def main() -> int:
    parser = argparse.ArgumentParser(description="Build TibiaSweden events.json from Tibia.com eventcalendar.")
    parser.add_argument("--months-back", type=int, default=1, help="How many months back from current month.")
    parser.add_argument("--months-forward", type=int, default=10, help="How many months forward from current month.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retries per month fetch.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON with indent=2.")
    parser.add_argument("--out", type=str, default="docs/events.json", help="Output file path.")
    parser.add_argument(
        "--upcoming-days",
        type=int,
        default=30,
        help="How many days ahead to list as upcoming.",
    )
    args = parser.parse_args()

    now = now_stockholm()
    year = now.year
    month = now.month

    months: List[Tuple[int, int]] = []
    for d in range(-args.months_back, args.months_forward + 1):
        y, m = add_months(year, month, d)
        months.append((y, m))

    session = requests.Session()
    # Use a desktop UA (sometimes helps avoid weird Tibia.com behavior)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        }
    )

    schedules: Dict[Tuple[int, int], "EventSchedule"] = {}
    for (y, m) in months:
        schedules[(y, m)] = fetch_schedule(m, y, session=session, timeout=args.timeout, retries=args.retries)

    occurrences, min_date, max_date = build_occurrences(schedules, months)
    today = now.date()

    happening, upcoming = compute_happening_and_upcoming(occurrences, today=today, upcoming_days=args.upcoming_days)

    events_out = []
    for o in occurrences:
        events_out.append(
            {
                "id": o.id,
                "title": o.title,
                "slug": slugify(o.title),
                "category": category_hint(o.title, o.description),
                "start": safe_iso_date(o.start),
                "end": safe_iso_date(o.end),
                "description": o.description,
                "color": o.color,
                "tibiaUrl": event_schedule_url(month=o.start.month, year=o.start.year),
            }
        )

    payload = {
        "meta": {
            "generatedAt": now.isoformat(),
            "timezone": STOCKHOLM_TZ,
            "source": "tibia.com eventcalendar",
            "range": {"min": min_date.isoformat(), "max": max_date.isoformat()},
            "months": [{"year": y, "month": m, "url": event_schedule_url(month=m, year=y)} for (y, m) in months],
            "upcomingDays": args.upcoming_days,
        },
        "today": today.isoformat(),
        "happeningNow": [o.id for o in happening],
        "upcoming": [o.id for o in upcoming],
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
