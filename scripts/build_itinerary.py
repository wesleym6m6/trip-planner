"""
Build itinerary.json from simplified agent input + places_cache.json.

Agent only provides: name, type, time, note (+ optional title, lat, lng).
Script auto-populates: place_id, lat, lng, maps_query, display_name from cache.

Usage:
  echo '<json>' | direnv exec $REPO python3 scripts/build_itinerary.py

Input format (stdin JSON):
{
  "cache_path": "trips/tainan-2026-04/data/places_cache.json",
  "output_path": "trips/tainan-2026-04/data/itinerary.json",
  "days": [
    {
      "day": 1,
      "date": "2026-04-17",
      "title": "Day title",
      "subtitle": "Day subtitle",
      "places": [
        {"name": "奇美博物館", "type": "spot", "time": "09:30", "note": "說明"},
        {"name": "奇美博物館", "type": "food", "time": "12:00", "note": "午餐", "title": "奇美博物館內午餐"},
        {"name": "森根", "type": "food", "time": "18:15", "note": "老宅義式", "lat": 22.9898, "lng": 120.2088}
      ]
    }
  ]
}

Place matching rules:
  1. "name" is used to fuzzy-match against places_cache.json
  2. Match priority: exact display_name > substring in display_name > substring in maps_query
  3. If matched: place_id, lat, lng, maps_query, display_name auto-populated from cache
  4. If NOT matched but "lat" + "lng" provided: place_id=null, coordinate-based Maps link
  5. If NOT matched and no lat/lng: ERROR (agent must fix)

Optional overrides:
  - "title": display title (default: "name")
  - "lat"/"lng": manual coordinates (skips cache lookup, sets place_id=null)
  - "place_id": explicit override (rare)
"""
import json
import sys
from pathlib import Path

if __package__:
    from .plan_compat import CanonicalWriteRefused, refuse_canonical_write
else:
    from plan_compat import CanonicalWriteRefused, refuse_canonical_write


def load_cache(path):
    with open(path) as f:
        return json.load(f)


def build_lookup(cache):
    """Build multiple lookup indices from cache."""
    import unicodedata
    by_exact_name = {}  # display_name (lowered, NFC) -> (place_id, entry)
    entries = []  # (place_id, entry) for substring search

    for pid, entry in cache.items():
        dn = entry.get("display_name", "")
        by_exact_name[unicodedata.normalize("NFC", dn.lower())] = (pid, entry)
        entries.append((pid, entry))

    return by_exact_name, entries


def match_place(name, by_exact_name, entries):
    """Find a cache entry matching the given name.

    Returns (place_id, entry) or (None, None).
    """
    import unicodedata
    key = unicodedata.normalize("NFC", name.lower().strip())

    # 1. Exact match on display_name
    if key in by_exact_name:
        return by_exact_name[key]

    # 2. Substring: name contained in display_name
    for pid, entry in entries:
        dn = unicodedata.normalize("NFC", entry.get("display_name", "").lower())
        if key in dn:
            return pid, entry

    # 3. Substring: name contained in maps_query
    for pid, entry in entries:
        mq = unicodedata.normalize("NFC", entry.get("maps_query", "").lower())
        if key in mq:
            return pid, entry

    # 4. Reverse: display_name contained in name
    for pid, entry in entries:
        dn = unicodedata.normalize("NFC", entry.get("display_name", "").lower())
        if dn and dn in key:
            return pid, entry

    return None, None


def build_place_entry(place_input, by_exact_name, entries):
    """Build a complete itinerary place entry from simplified input."""
    name = place_input["name"]
    has_manual_coords = place_input.get("lat") and place_input.get("lng")

    if has_manual_coords:
        # Manual coordinates provided — skip cache lookup
        return {
            "type": place_input["type"],
            "title": place_input.get("title", name),
            "note": place_input.get("note", ""),
            "maps_query": f"{place_input['lat']},{place_input['lng']}",
            "place_id": place_input.get("place_id"),  # usually null
            "lat": place_input["lat"],
            "lng": place_input["lng"],
            "display_name": place_input.get("title", name),
            "time": place_input.get("time"),
        }

    # Try to match against cache
    pid, entry = match_place(name, by_exact_name, entries)

    if not pid:
        return {"error": f"No cache match for '{name}' and no lat/lng provided"}

    # If cache key starts with "manual_", it's not a real Google place_id.
    # Set to null so the template uses coordinate-based Maps link.
    resolved_pid = place_input.get("place_id", pid)
    if resolved_pid and str(resolved_pid).startswith("manual_"):
        resolved_pid = None

    return {
        "type": place_input["type"],
        "title": place_input.get("title", name),
        "note": place_input.get("note", ""),
        "maps_query": entry.get("maps_query", ""),
        "place_id": resolved_pid,
        "lat": entry["lat"],
        "lng": entry["lng"],
        "display_name": entry.get("display_name", name),
        "time": place_input.get("time"),
    }


def main():
    input_data = json.load(sys.stdin)
    cache_path = input_data["cache_path"]
    output_path = input_data.get("output_path")

    if output_path:
        try:
            refuse_canonical_write(
                Path(output_path).parent, operation="build_itinerary.py"
            )
        except CanonicalWriteRefused as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(2)

    cache = load_cache(cache_path)
    by_exact_name, entries = build_lookup(cache)

    itinerary = {"days": []}
    errors = []

    for day_input in input_data["days"]:
        day = {
            "day": day_input["day"],
            "date": day_input.get("date", ""),
            "title": day_input.get("title", ""),
            "subtitle": day_input.get("subtitle", ""),
            "places": [],
        }

        for i, place_input in enumerate(day_input["places"]):
            result = build_place_entry(place_input, by_exact_name, entries)

            if "error" in result:
                errors.append(f"Day {day_input['day']} [{i}] {place_input['name']}: {result['error']}")
                continue

            day["places"].append(result)

        itinerary["days"].append(day)

    if errors:
        print("ERRORS — these places could not be resolved:", file=sys.stderr)
        for e in errors:
            print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)

    if output_path:
        with open(output_path, "w") as f:
            json.dump(itinerary, f, ensure_ascii=False, indent=2)
        print(f"Written: {output_path}")
    else:
        json.dump(itinerary, sys.stdout, ensure_ascii=False, indent=2)

    # Summary
    total = sum(len(d["places"]) for d in itinerary["days"])
    matched = sum(1 for d in itinerary["days"] for p in d["places"] if p.get("place_id"))
    manual = total - matched
    print(f"Done: {total} places ({matched} from cache, {manual} manual coords)")


if __name__ == "__main__":
    main()
