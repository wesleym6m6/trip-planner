"""
Validate trip data integrity before rendering.

Exports validate(trip_dir) for programmatic use.
CLI: python scripts/validate_trip.py trips/{slug}
     exit 0 = pass, exit 1 = errors found
"""
import json
import sys
from pathlib import Path

if __package__:
    from .plan_compat import PlanCodecError, has_canonical_plan, load_trip_views
else:
    from plan_compat import PlanCodecError, has_canonical_plan, load_trip_views


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


def validate(trip_dir):
    """Validate trip data completeness. Returns list of error strings (empty = pass)."""
    trip_dir = Path(trip_dir)
    data_dir = trip_dir / "data"
    errors = []
    canonical = has_canonical_plan(data_dir)

    # --- Required files ---
    required_files = {"info.json": "practical info"}
    if not canonical:
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
    optional_files = ["reservations.json", "todo.json", "packing.json"]
    for filename in optional_files:
        if not (data_dir / filename).exists():
            print(f"  ℹ Optional file missing: {filename}", file=sys.stderr)

    # Stop early if required files missing
    if errors:
        return errors

    try:
        trip, itinerary, _trip_id, _revision = load_trip_views(data_dir)
    except PlanCodecError as exc:
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

            # Check time ordering within a day
            times = []
            for place in day.get("places", []):
                t = place.get("time")
                if t:
                    times.append(t)
            for i in range(1, len(times)):
                if times[i] < times[i - 1]:
                    errors.append(
                        f"{day_label}: time not ascending — "
                        f"'{times[i-1]}' then '{times[i]}' "
                        f"(places[{i-1}] → places[{i}])"
                    )

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
