"""
Read full detail for a specific entry from a SerpApi cache file.

The search scripts (search_flights.py, search_hotels.py) output key-field
summaries to stdout.  When the agent needs the FULL data for a specific
entry (e.g. all OTA rates, description, detailed nearby places), use this
script to extract it from the cache without loading the entire file into
context.

Usage:
    python3 scripts/cache_detail.py <cache_path> <index_or_name>

Examples:
    # By index (from the summary's "index" field)
    python3 scripts/cache_detail.py trips/{slug}/data/hotels_cache.json 3

    # By name substring (case-insensitive)
    python3 scripts/cache_detail.py trips/{slug}/data/hotels_cache.json "Example Hotel"

    # Flights — by index or airline name
    python3 scripts/cache_detail.py trips/{slug}/data/flights_cache.json 0
    python3 scripts/cache_detail.py trips/{slug}/data/flights_cache.json "Example Airline"
"""
import json
import sys


def collect_items(data):
    """Gather list items from known SerpApi cache structures."""
    items = []
    # Hotels
    if "properties" in data and isinstance(data["properties"], list):
        items.extend(data["properties"])
    # Flights (best + other)
    for key in ("best_flights", "other_flights"):
        if key in data and isinstance(data[key], list):
            items.extend(data[key])
    return items


def item_name(item):
    """Best-effort human name for an item (hotel name or airline+flight)."""
    if "name" in item:
        return item["name"]
    legs = item.get("flights", [])
    if legs:
        first = legs[0]
        return f"{first.get('airline', '?')} {first.get('flight_number', '')}"
    return "?"


def match_by_name(items, query):
    """Return list of (index, item) tuples matching query substring."""
    q = query.lower()
    matches = []
    for i, item in enumerate(items):
        searchable = item_name(item).lower()
        # Also check airline names inside flight legs
        for leg in item.get("flights", []):
            searchable += " " + leg.get("airline", "").lower()
        if q in searchable:
            matches.append((i, item))
    return matches


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(1)

    cache_path = sys.argv[1]
    query = sys.argv[2]

    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)

    items = collect_items(data)
    if not items:
        print("No properties or flights found in cache", file=sys.stderr)
        sys.exit(1)

    # Try numeric index first
    try:
        idx = int(query)
        if 0 <= idx < len(items):
            json.dump(items[idx], sys.stdout, ensure_ascii=False, indent=2)
            print()
            return
        print(f"Index {idx} out of range (0-{len(items) - 1})", file=sys.stderr)
        sys.exit(1)
    except ValueError:
        pass

    # Name/substring match
    matches = match_by_name(items, query)

    if not matches:
        print(f"No match for '{query}' in {len(items)} items", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"Multiple matches ({len(matches)}) — returning first:", file=sys.stderr)
        for idx, item in matches:
            print(f"  [{idx}] {item_name(item)}", file=sys.stderr)

    json.dump(matches[0][1], sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
