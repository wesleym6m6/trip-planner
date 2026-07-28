"""
Validate trip data integrity before rendering.

Exports validate(trip_dir) for programmatic use.
CLI: python scripts/validate_trip.py trips/{slug}
     exit 0 = pass, exit 1 = errors found
"""
import json
import sys
from datetime import time as local_time
from pathlib import Path

if __package__:
    from .plan_compat import (
        PlanCodecError,
        has_canonical_plan,
        load_trip_views,
        resolve_ordered_local_datetimes,
    )
else:
    from plan_compat import (
        PlanCodecError,
        has_canonical_plan,
        load_trip_views,
        resolve_ordered_local_datetimes,
    )


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _check_keys(obj, required_keys, context):
    """Check that obj (dict) has all required_keys. Returns list of error strings."""
    errors = []
    for key in required_keys:
        if key not in obj or obj[key] is None:
            errors.append(f"{context}: missing required field '{key}'")
    return errors


def _parse_local_time(value):
    if isinstance(value, local_time):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = local_time.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is None else None


def validate(trip_dir):
    """Validate trip data completeness. Returns list of error strings (empty = pass)."""
    trip_dir = Path(trip_dir)
    data_dir = trip_dir / "data"
    errors = []
    canonical = has_canonical_plan(data_dir)

    # --- Required files ---
    # A canonical plan replaces only trip.json and itinerary.json; the five
    # renderer sidecars remain required in either supported data mode.
    required_files = {
        "reservations.json": "reservations",
        "todo.json": "pre-trip checklist",
        "info.json": "practical info",
        "packing.json": "packing list",
        "places_cache.json": "places cache",
    }
    if canonical:
        required_files["plan.json"] = "canonical plan"
    else:
        required_files.update(
            {
                "trip.json": "trip metadata",
                "itinerary.json": "daily itinerary",
            }
        )
    for filename, desc in required_files.items():
        if not (data_dir / filename).exists():
            errors.append(f"Missing required file: {filename} ({desc})")

    # --- Optional files (warn only) ---
    optional_files = ["flights_cache.json", "hotels_cache.json"]
    for filename in optional_files:
        if not (data_dir / filename).exists():
            print(f"  ℹ Optional file missing: {filename}", file=sys.stderr)

    # Stop early if required files missing
    if errors:
        return errors

    try:
        trip, itinerary, _trip_id, _revision = load_trip_views(data_dir)
    except (OSError, PlanCodecError) as exc:
        errors.append(f"plan.json: {exc}")
        return errors

    trip_label = "plan.json state.trip" if canonical else "trip.json"
    itinerary_label = (
        "plan.json state.itinerary" if canonical else "itinerary.json"
    )

    # --- trip metadata ---
    errors.extend(_check_keys(trip, ["title", "slug", "date_range", "cities", "icon"],
                               trip_label))

    # --- itinerary ---
    if "days" not in itinerary:
        errors.append(f"{itinerary_label}: missing 'days' array")
    else:
        for day_idx, day in enumerate(itinerary["days"]):
            day_label = f"{itinerary_label} day[{day_idx}]"
            for place_idx, place in enumerate(day.get("places", [])):
                place_label = f"{day_label}.places[{place_idx}]"
                errors.extend(_check_keys(place, ["type", "title", "time", "lat", "lng"],
                                           place_label))

            # Use the shared rollover resolver, but only permit a backwards
            # wall clock when the day explicitly declares a valid overnight
            # availability window.
            places = day.get("places", [])
            raw_times = tuple(place.get("time") for place in places)
            resolved_times = resolve_ordered_local_datetimes(
                day.get("date"),
                raw_times,
                available_start=day.get("available_start"),
                available_end=day.get("available_end"),
            )
            available_start = _parse_local_time(day.get("available_start"))
            available_end = _parse_local_time(day.get("available_end"))
            overnight_window = (
                available_start is not None
                and available_end is not None
                and available_end <= available_start
            )
            previous = None
            for place_idx, (raw_time, resolved_time) in enumerate(
                zip(raw_times, resolved_times)
            ):
                parsed_time = _parse_local_time(raw_time)
                if parsed_time is None:
                    continue
                if previous is not None and parsed_time < previous[1]:
                    rollover_resolved = (
                        overnight_window
                        and previous[1] >= available_start
                        and parsed_time <= available_end
                        and previous[2] is not None
                        and resolved_time is not None
                        and resolved_time > previous[2]
                        and resolved_time.date() > previous[2].date()
                    )
                    if not rollover_resolved:
                        errors.append(
                            f"{day_label}: time not ascending — "
                            f"'{previous[0]}' then '{raw_time}' "
                            f"(places[{previous[3]}] → places[{place_idx}])"
                        )
                previous = (raw_time, parsed_time, resolved_time, place_idx)

    # --- info.json ---
    info = _load_json(data_dir / "info.json")
    sections = info if isinstance(info, list) else info.get("sections", [])
    for sec_idx, section in enumerate(sections):
        sec_label = f"info.json sections[{sec_idx}]"
        errors.extend(_check_keys(section, ["title", "type"], sec_label))
        if section.get("type") not in ("table", "text", None):
            errors.append(f"{sec_label}: type must be 'table' or 'text', got '{section.get('type')}'")

    # --- reservations.json (if exists) ---
    res_path = data_dir / "reservations.json"
    if res_path.exists():
        reservations = _load_json(res_path)
        for i, item in enumerate(reservations):
            errors.extend(_check_keys(item, ["label", "note"], f"reservations.json[{i}]"))

    # --- todo.json (if exists) ---
    todo_path = data_dir / "todo.json"
    if todo_path.exists():
        todos = _load_json(todo_path)
        for i, item in enumerate(todos):
            errors.extend(_check_keys(item, ["label", "hint"], f"todo.json[{i}]"))

    # --- packing.json (if exists) ---
    packing_path = data_dir / "packing.json"
    if packing_path.exists():
        packing = _load_json(packing_path)
        for i, item in enumerate(packing):
            errors.extend(_check_keys(item, ["label", "category"], f"packing.json[{i}]"))

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/validate_trip.py trips/{slug}", file=sys.stderr)
        sys.exit(1)

    trip_dir = Path(sys.argv[1])
    if not trip_dir.exists():
        print(f"Trip directory not found: {trip_dir}", file=sys.stderr)
        sys.exit(1)

    errors = validate(trip_dir)

    if errors:
        print(f"Validation FAILED ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  \u2717 {e}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Validation PASSED: {trip_dir}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
