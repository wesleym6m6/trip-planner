"""
Generate an ICS calendar file from a trip's itinerary.json and trip.json.

Usage: python3 scripts/generate_ics.py trips/vietnam-2026-05
Output: trips/vietnam-2026-05/calendar.ics
"""
import json
import sys
import pathlib
from datetime import datetime, timedelta, timezone

if __package__:
    from .plan_compat import (
        has_canonical_plan,
        load_trip_views,
        resolve_ordered_local_datetimes,
    )
else:
    from plan_compat import (
        has_canonical_plan,
        load_trip_views,
        resolve_ordered_local_datetimes,
    )


def ics_escape(s: str) -> str:
    """Escape text per RFC 5545 rules."""
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def fold_line(line: str) -> str:
    """Fold a content line so no line exceeds 75 octets (RFC 5545 Section 3.1).

    The first line can be up to 75 octets. Continuation lines start with a
    single space and can hold up to 74 octets of content (75 total with the
    leading space).
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line

    parts = []
    # First segment: up to 75 octets
    chunk = _safe_slice(encoded, 0, 75)
    parts.append(chunk.decode("utf-8"))
    pos = len(chunk)

    # Continuation segments: up to 74 octets of content (+ 1 leading space = 75)
    while pos < len(encoded):
        chunk = _safe_slice(encoded, pos, 74)
        parts.append(" " + chunk.decode("utf-8"))
        pos += len(chunk)

    return "\r\n".join(parts)


def _safe_slice(data: bytes, start: int, max_bytes: int) -> bytes:
    """Slice bytes without splitting a multi-byte UTF-8 character."""
    end = min(start + max_bytes, len(data))
    # If we're at the end, just take everything
    if end >= len(data):
        return data[start:]
    # Walk back if we landed in the middle of a multi-byte sequence
    while end > start and (data[end] & 0xC0) == 0x80:
        end -= 1
    return data[start:end]


def parse_date_range(date_range: str) -> datetime | None:
    """Parse a date_range string like '2026-05-15 ~ 2026-05-17' or
    '2026/05/15 – 2026/05/24' and return the start date."""
    # Normalize separators
    s = date_range.strip()
    # Split on common range delimiters
    for delim in ["~", "–", "-", "—"]:
        # Only split on delimiters that are surrounded by spaces (to avoid
        # splitting date hyphens like 2026-05-15)
        parts = s.split(f" {delim} ")
        if len(parts) == 2:
            start_str = parts[0].strip()
            # Try parsing with different formats
            for fmt in ["%Y-%m-%d", "%Y/%m/%d"]:
                try:
                    return datetime.strptime(start_str, fmt)
                except ValueError:
                    continue
    return None


def generate_ics(trip_dir: str | pathlib.Path) -> pathlib.Path:
    """Generate a calendar.ics file for a trip directory.

    Returns the path to the generated .ics file.
    """
    trip_dir = pathlib.Path(trip_dir)
    data_dir = trip_dir / "data"

    trip, itinerary, _trip_id, _revision = load_trip_views(data_dir)

    slug = trip.get("slug", trip_dir.name)
    title = trip.get("title", slug)
    date_range = trip.get("date_range", "")

    start_date = parse_date_range(date_range)
    if start_date is None:
        # Fallback: use today as Day 1
        start_date = datetime.now()

    now_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TripPlan//TripPlan//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(title)}",
    ]

    # Detect UTC offset from places_cache for DTSTART/DTEND timezone
    utc_offset_str = None
    cache_path = data_dir / "places_cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        for entry in cache.values():
            offset = entry.get("utc_offset_minutes")
            if offset is not None:
                sign = "+" if offset >= 0 else "-"
                h, m = divmod(abs(offset), 60)
                utc_offset_str = f"{sign}{h:02d}{m:02d}"
                break

    for day in itinerary.get("days", []):
        day_num = day["day"]
        day_title = day.get("title", f"Day {day_num}")
        day_date = start_date + timedelta(days=day_num - 1)
        places = day.get("places", [])
        resolved_times = resolve_ordered_local_datetimes(
            day_date,
            (place.get("time") for place in places),
            available_start=day.get("available_start"),
            available_end=day.get("available_end"),
        )

        # Per-place events (when time is available and timezone is known)
        if utc_offset_str:
            for i, place in enumerate(places):
                start_dt = resolved_times[i]
                if start_dt is None:
                    continue
                p_title = place.get("title", "")
                p_note = place.get("note", "")
                dt_start = (
                    f"{start_dt.strftime('%Y%m%dT%H%M%S')}"
                    f"{utc_offset_str}"
                )

                # End time: use next place's time, or +1h default
                next_time = (
                    resolved_times[i + 1]
                    if i + 1 < len(places)
                    else None
                )
                if next_time is not None:
                    dt_end = (
                        f"{next_time.strftime('%Y%m%dT%H%M%S')}"
                        f"{utc_offset_str}"
                    )
                else:
                    end_dt = start_dt + timedelta(hours=1)
                    dt_end = (
                        f"{end_dt.strftime('%Y%m%dT%H%M%S')}"
                        f"{utc_offset_str}"
                    )

                summary = ics_escape(f"{p_title}")
                description = ics_escape(p_note) if p_note else ""

                lines.append("BEGIN:VEVENT")
                lines.append(f"UID:trip-d{day_num}p{i}-{slug}@tripplan")
                lines.append(f"DTSTAMP:{now_utc}")
                lines.append(f"DTSTART:{dt_start}")
                lines.append(f"DTEND:{dt_end}")
                lines.append(f"SUMMARY:{summary}")
                if description:
                    lines.append(f"DESCRIPTION:{description}")
                lines.append("END:VEVENT")
        else:
            # Fallback: all-day event per day (no timezone info)
            place_names = []
            for p in places:
                t = p.get("time", "")
                prefix = f"{t} " if t else ""
                place_names.append(f"{prefix}{p['title']}")
            description = "\\n".join(ics_escape(name) for name in place_names)
            summary = ics_escape(f"Day {day_num} — {day_title}")

            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:trip-day{day_num}-{slug}@tripplan")
            lines.append(f"DTSTAMP:{now_utc}")
            lines.append(f"DTSTART;VALUE=DATE:{day_date_str}")
            lines.append(f"DTEND;VALUE=DATE:{next_date_str}")
            lines.append(f"SUMMARY:{summary}")
            lines.append(f"DESCRIPTION:{description}")
            lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    # Fold long lines and join with CRLF
    folded_lines = [fold_line(line) for line in lines]
    ics_content = "\r\n".join(folded_lines) + "\r\n"

    output_path = trip_dir / "calendar.ics"
    output_path.write_text(ics_content, encoding="utf-8")
    print(f"Generated: {output_path}")
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/generate_ics.py trips/<slug>")
        sys.exit(1)

    trip_dir = pathlib.Path(sys.argv[1])
    data_dir = trip_dir / "data"
    if not has_canonical_plan(data_dir) and not (data_dir / "trip.json").exists():
        print(f"Error: no plan.json or trip.json found under {data_dir}")
        sys.exit(1)

    generate_ics(trip_dir)


if __name__ == "__main__":
    main()
