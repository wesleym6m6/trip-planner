"""
Search hotels via SerpApi Google Hotels engine.

Input (stdin JSON):
{
  "q": "Example City hotels",
  "check_in_date": "2099-01-01",
  "check_out_date": "2099-01-05",
  "cache_path": "trips/{slug}/data/hotels_cache.json"
}

Required: q, check_in_date, check_out_date, cache_path

Optional overrides: adults, currency, hl, gl, sort_by, hotel_class,
  min_price, max_price, rating, amenities, free_cancellation,
  special_offers, eco_certified, brands, vacation_rentals,
  children, children_ages, next_page_token, property_token

Full parameter docs: docs/serpapi-hotels-params.md
"""
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from serpapi_common import add_fetched_at, serpapi_search, strip_images, write_cache

DEFAULTS = {
    "adults": 2,
    "currency": "TWD",
    "hl": "zh-TW",
}

REQUIRED_FIELDS = {"q", "check_in_date", "check_out_date", "cache_path", "gl"}

OPTIONAL_PARAMS = {
    "adults", "currency", "hl", "sort_by", "hotel_class",
    "min_price", "max_price", "rating", "amenities", "free_cancellation",
    "special_offers", "eco_certified", "brands", "vacation_rentals",
    "children", "children_ages", "next_page_token", "property_token",
}


def build_params(user_input):
    """Merge DEFAULTS + user_input into SerpApi params."""
    merged = {**DEFAULTS, **user_input}

    params = {"engine": "google_hotels"}

    # Required fields
    for key in ("q", "check_in_date", "check_out_date", "gl"):
        params[key] = merged[key]

    # Optional params
    for key in OPTIONAL_PARAMS:
        if key in merged and merged[key] is not None:
            params[key] = merged[key]

    return params


def extract_hotels(raw):
    """Extract hotel data, removing search_metadata/search_parameters."""
    result = {}
    for key, value in raw.items():
        if key in ("search_metadata", "search_parameters"):
            continue
        result[key] = value
    return result


def mark_cheapest_ota(properties):
    """For each hotel, mark the cheapest OTA source in nearby_places or rate info."""
    for prop in properties:
        rate = prop.get("rate_per_night", {})
        lowest = rate.get("extracted_lowest")
        if lowest is not None:
            prop["cheapest_rate"] = lowest
            prop["cheapest_source"] = rate.get("source", prop.get("name", "unknown"))
    return properties


def summarize_property(prop, index):
    """Extract key fields for stdout summary (keeps cache intact)."""
    rate = prop.get("rate_per_night", {})
    total = prop.get("total_rate", {})
    nearby = []
    for p in prop.get("nearby_places", [])[:3]:
        transport = p.get("transportations", [{}])
        duration = transport[0].get("duration", "?") if transport else "?"
        nearby.append({"name": p.get("name", "?"), "duration": duration})
    coords = prop.get("gps_coordinates", {})
    result = {
        "index": index,
        "name": prop.get("name", "?"),
        "rate": rate.get("extracted_lowest"),
        "total": total.get("extracted_lowest"),
        "rating": prop.get("overall_rating"),
        "reviews": prop.get("reviews"),
        "class": prop.get("hotel_class", ""),
        "amenities": prop.get("amenities", [])[:6],
        "nearby": nearby,
        "check_in_time": prop.get("check_in_time"),
        "check_out_time": prop.get("check_out_time"),
    }
    if coords:
        result["lat"] = coords.get("latitude")
        result["lng"] = coords.get("longitude")
    deal = prop.get("deal_description")
    if deal:
        result["deal"] = deal
    return result


def main():
    user_input = json.load(sys.stdin)

    missing = REQUIRED_FIELDS - set(user_input.keys())
    if missing:
        print(f"Missing required fields: {', '.join(sorted(missing))}", file=sys.stderr)
        sys.exit(1)

    cache_path = user_input.pop("cache_path")

    params = build_params(user_input)
    print(f"Searching hotels: {params.get('q')} "
          f"({params.get('check_in_date')} ~ {params.get('check_out_date')})", file=sys.stderr)

    raw = serpapi_search(params)
    data = extract_hotels(raw)

    properties = data.get("properties", [])

    # Strip images (~760 lines saved)
    properties = strip_images(properties)

    # Filter sponsored results
    properties = [p for p in properties if not p.get("sponsored")]

    # Filter hotels without price
    properties = [p for p in properties
                  if p.get("rate_per_night", {}).get("extracted_lowest") is not None]

    # Add fetched_at
    add_fetched_at(properties)

    # Mark cheapest OTA
    mark_cheapest_ota(properties)

    data["properties"] = properties

    write_cache(cache_path, data)

    # Summary with per-property key fields
    prices = [p.get("rate_per_night", {}).get("extracted_lowest", 0)
              for p in properties if p.get("rate_per_night", {}).get("extracted_lowest") is not None]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = {
        "hotels_count": len(properties),
        "price_range": [min(prices), max(prices)] if prices else [],
        "cache_path": cache_path,
        "fetched_at": now,
        "properties": [summarize_property(p, i) for i, p in enumerate(properties)],
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
