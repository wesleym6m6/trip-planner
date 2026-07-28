"""
Check opening hours conflicts for all places in an itinerary.

For each place on each day, checks:
1. Whether the place is open on that day of the week
2. Whether the scheduled visit time falls within opening hours

Uses data from places_cache.json, itinerary.json times, and trip.json dates.

Usage:
    direnv exec $REPO python3 scripts/check_hours.py trips/{slug}

Statuses:
  ✅  Visit time is within opening hours
  ⚠️  Day is open but visit time is outside hours (too early / too late / break)
  ❌  Closed on this day of the week
  🔓  Outdoor / public space (always accessible)
  ❓  No opening hours data from API
"""
import json
import re
import sys
from datetime import datetime, time as local_time, timedelta
from pathlib import Path

if __package__:
    from .plan_compat import (
        load_trip_views,
        resolve_ordered_local_datetimes,
    )
else:
    from plan_compat import (
        load_trip_views,
        resolve_ordered_local_datetimes,
    )

OUTDOOR_TYPES = {
    "street", "park", "neighborhood", "natural_feature", "bridge",
    "locality", "sublocality", "route", "intersection", "premise",
}

DOW_NAMES_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DOW_NAMES_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]

# Python weekday (0=Mon..6=Sun) → Google day (0=Sun, 1=Mon..6=Sat)
PY_TO_GOOGLE_DOW = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}


def parse_date_range(date_range_str):
    match = re.match(r"(\d{4}-\d{2}-\d{2})", date_range_str)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d")


def get_day_hours_str(opening_hours, weekday_idx):
    """Get human-readable hours string for a weekday (Python index)."""
    if not opening_hours:
        return None, None
    descriptions = opening_hours.get("weekdayDescriptions", [])
    if not descriptions or weekday_idx >= len(descriptions):
        return None, None
    day_str = descriptions[weekday_idx]
    parts = day_str.split(": ", 1)
    if len(parts) < 2:
        return day_str, False
    hours = parts[1]
    is_closed = hours.strip().lower() == "closed"
    return hours, is_closed


def get_periods_for_day(opening_hours, google_dow):
    """Get all (open_min, close_min) periods for a Google day-of-week."""
    if not opening_hours:
        return []
    periods = []
    for p in opening_hours.get("periods", []):
        if p.get("open", {}).get("day") != google_dow:
            continue
        open_h = p["open"].get("hour", 0)
        open_m = p["open"].get("minute", 0)
        close_day = p.get("close", {}).get("day", google_dow)
        close_h = p.get("close", {}).get("hour", 23)
        close_m = p.get("close", {}).get("minute", 59)

        open_min = open_h * 60 + open_m
        close_min = close_h * 60 + close_m
        if close_day != google_dow:
            close_min += 24 * 60  # overnight

        periods.append((open_min, close_min, f"{open_h:02d}:{open_m:02d}", f"{close_h:02d}:{close_m:02d}"))

    periods.sort(key=lambda x: x[0])
    return periods


def check_visit_time(periods, visit_time_str):
    """Check if visit_time falls within any period.

    Returns (status, detail_str).
    """
    if not visit_time_str or not periods:
        return None, None

    try:
        parsed = local_time.fromisoformat(visit_time_str)
    except (TypeError, ValueError):
        return None, None
    if parsed.tzinfo is not None:
        return None, None
    visit_min = (
        parsed.hour * 60
        + parsed.minute
        + parsed.second / 60
        + parsed.microsecond / 60_000_000
    )

    # Check if visit falls in any period
    for open_min, close_min, open_str, close_str in periods:
        if open_min <= visit_min <= close_min:
            return "in_range", f"{open_str}-{close_str}"

    # Not in any period — classify why
    all_ranges = " / ".join(f"{o}-{c}" for _, _, o, c in periods)

    first_open = periods[0][0]
    if visit_min < first_open:
        wait = first_open - visit_min
        return "early", f"{visit_time_str} 到但 {periods[0][2]} 才開門（早到 {wait} min）"

    last_close = periods[-1][1] % (24 * 60)  # normalize overnight
    if visit_min > last_close:
        late = visit_min - last_close
        return "late", f"{visit_time_str} 到但 {periods[-1][3]} 已關門（遲到 {late} min）"

    # Between periods (lunch break etc.)
    for i in range(len(periods) - 1):
        if periods[i][1] < visit_min < periods[i + 1][0]:
            wait = periods[i + 1][0] - visit_min
            return "break", f"{visit_time_str} 在休息時段，{periods[i+1][2]} 重新開放（等 {wait} min）"

    return None, None


def is_outdoor_type(types):
    if not types:
        return False
    return bool(set(types) & OUTDOOR_TYPES)


def check_place(place, cache, weekday_idx):
    """Check a single place's opening hours for a given day and visit time."""
    place_id = place.get("place_id")
    title = place.get("title", "?")
    visit_time = place.get("time")

    if place.get("type") in ("flight", "transport"):
        return None

    cache_entry = cache.get(place_id, {}) if place_id else {}
    opening_hours = cache_entry.get("regular_opening_hours")
    types = cache_entry.get("types", [])

    # Step 1: Check day-of-week
    hours_str, is_closed = get_day_hours_str(opening_hours, weekday_idx)

    if is_closed:
        return {
            "title": title,
            "place_id": place_id,
            "time": visit_time,
            "status": "❌",
            "hours": "Closed",
            "note": "當天公休",
        }

    # No hours data
    if not hours_str:
        if is_outdoor_type(types):
            return {
                "title": title,
                "place_id": place_id,
                "time": visit_time,
                "status": "🔓",
                "hours": None,
                "note": "戶外/公共空間",
            }
        return {
            "title": title,
            "place_id": place_id,
            "time": visit_time,
            "status": "❓",
            "hours": None,
            "note": "無營業時間資料",
        }

    # Step 2: Check specific visit time against periods
    google_dow = PY_TO_GOOGLE_DOW[weekday_idx]
    periods = get_periods_for_day(opening_hours, google_dow)

    if visit_time and periods:
        time_status, detail = check_visit_time(periods, visit_time)

        if time_status == "in_range":
            return {
                "title": title,
                "place_id": place_id,
                "time": visit_time,
                "status": "✅",
                "hours": hours_str,
                "note": f"{visit_time} 在 {detail} 內",
            }
        elif time_status in ("early", "late", "break"):
            return {
                "title": title,
                "place_id": place_id,
                "time": visit_time,
                "status": "⚠️",
                "hours": hours_str,
                "note": detail,
            }

    # Has hours but no visit time or no periods — just confirm day is open
    return {
        "title": title,
        "place_id": place_id,
        "time": visit_time,
        "status": "✅",
        "hours": hours_str,
        "note": None,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/check_hours.py trips/{slug}", file=sys.stderr)
        sys.exit(1)

    trip_dir = Path(sys.argv[1])
    data_dir = trip_dir / "data"

    trip_json, itinerary, _trip_id, _revision = load_trip_views(data_dir)
    cache_path = data_dir / "places_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    start_date = parse_date_range(trip_json.get("date_range", ""))
    if not start_date:
        print("ERROR: Cannot parse date_range from trip.json", file=sys.stderr)
        sys.exit(1)

    checks = []
    warnings = []

    for day_data in itinerary.get("days", []):
        day_num = day_data["day"]
        current_date = start_date + timedelta(days=day_num - 1)
        weekday_idx = current_date.weekday()
        places = day_data.get("places", [])
        resolved_times = resolve_ordered_local_datetimes(
            current_date,
            (place.get("time") for place in places),
            available_start=day_data.get("available_start"),
            available_end=day_data.get("available_end"),
        )

        day_check = {
            "day": day_num,
            "date": current_date.strftime("%Y-%m-%d"),
            "day_of_week": DOW_NAMES_EN[weekday_idx],
            "day_of_week_zh": DOW_NAMES_ZH[weekday_idx],
            "places": [],
        }

        for place, resolved_at in zip(places, resolved_times):
            place_weekday_idx = (
                resolved_at.weekday()
                if resolved_at is not None
                else weekday_idx
            )
            result = check_place(place, cache, place_weekday_idx)
            if result is None:
                continue
            result["visit_date"] = (
                resolved_at.date().isoformat()
                if resolved_at is not None
                else current_date.date().isoformat()
            )
            day_check["places"].append(result)

            if result["status"] in ("⚠️", "❌"):
                warnings.append(
                    f"{result['status']} {result['title']} — Day {day_num}（{DOW_NAMES_ZH[place_weekday_idx]}）{result.get('note', '')}"
                )

        checks.append(day_check)

    output = {"checks": checks, "warnings": warnings}
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)

    # Human-readable summary
    print(f"\n營業時間檢查結果（{trip_json.get('date_range', '?')}）", file=sys.stderr)
    print("=" * 50, file=sys.stderr)
    for day_check in checks:
        print(f"\nDay {day_check['day']}（{day_check['day_of_week_zh']} {day_check['date']}）", file=sys.stderr)
        for p in day_check["places"]:
            time_str = f"[{p['time']}]" if p.get("time") else ""
            hours_display = p["hours"] or p.get("note", "")
            note = f" — {p['note']}" if p.get("note") and p["note"] != hours_display else ""
            print(f"  {p['status']} {time_str:>7} {p['title']}: {hours_display}{note}", file=sys.stderr)

    if warnings:
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"⚠️ 共 {len(warnings)} 個問題：", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)
    else:
        print(f"\n✅ 所有到達時間都在營業時間內", file=sys.stderr)


if __name__ == "__main__":
    main()
