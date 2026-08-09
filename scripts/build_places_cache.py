"""
Deprecated legacy cache builder.  Broad/full-mask Places collection is
quarantined and requires an explicit opt-in; it is never a Phase 4 runtime.

Input (stdin JSON):
{
  "candidates": [
    {"name": "Example Museum", "maps_query": "Example Museum, Example City"},
    ...
  ],
  "cache_path": "trips/{slug}/data/places_cache.json"
}

Loads existing cache (if any), skips already-cached place_ids, resolves new ones,
merges, and writes back. Append-only — never deletes cache entries.
"""
import json
import os
import sys
from datetime import datetime, timezone

_LEGACY_OPT_IN = "--legacy-full-mask-cache"


def transform_raw_to_cache(raw_place, maps_query):
    """Transform raw Places API response into flat cache entry."""
    loc = raw_place.get("location", {})
    display_name = raw_place.get("displayName", {})
    editorial = raw_place.get("editorialSummary", {})
    generative = raw_place.get("generativeSummary", {})
    primary_type_display = raw_place.get("primaryTypeDisplayName", {})

    return {
        "maps_query": maps_query,
        "display_name": display_name.get("text"),
        "types": raw_place.get("types"),
        "primary_type": raw_place.get("primaryType"),
        "primary_type_display_name": primary_type_display.get("text") if primary_type_display else None,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "formatted_address": raw_place.get("formattedAddress"),
        "short_address": raw_place.get("shortFormattedAddress"),
        "google_maps_uri": raw_place.get("googleMapsUri"),
        "google_maps_links": raw_place.get("googleMapsLinks"),
        "website": raw_place.get("websiteUri"),
        "international_phone": raw_place.get("internationalPhoneNumber"),
        "national_phone": raw_place.get("nationalPhoneNumber"),
        "rating": raw_place.get("rating"),
        "rating_count": raw_place.get("userRatingCount"),
        "price_level": raw_place.get("priceLevel"),
        "price_range": raw_place.get("priceRange"),
        "business_status": raw_place.get("businessStatus"),
        "regular_opening_hours": raw_place.get("regularOpeningHours"),
        "current_opening_hours": raw_place.get("currentOpeningHours"),
        "time_zone": raw_place.get("timeZone"),
        "utc_offset_minutes": raw_place.get("utcOffsetMinutes"),
        "editorial_summary": editorial.get("text") if editorial else None,
        "generative_summary": generative if generative else None,
        "reviews": raw_place.get("reviews"),
        "photos": raw_place.get("photos"),
        "payment_options": raw_place.get("paymentOptions"),
        "parking_options": raw_place.get("parkingOptions"),
        "accessibility_options": raw_place.get("accessibilityOptions"),
        "serves_breakfast": raw_place.get("servesBreakfast"),
        "serves_lunch": raw_place.get("servesLunch"),
        "serves_dinner": raw_place.get("servesDinner"),
        "serves_beer": raw_place.get("servesBeer"),
        "serves_wine": raw_place.get("servesWine"),
        "serves_brunch": raw_place.get("servesBrunch"),
        "serves_vegetarian_food": raw_place.get("servesVegetarianFood"),
        "serves_cocktails": raw_place.get("servesCocktails"),
        "serves_coffee": raw_place.get("servesCoffee"),
        "serves_dessert": raw_place.get("servesDessert"),
        "takeout": raw_place.get("takeout"),
        "delivery": raw_place.get("delivery"),
        "dine_in": raw_place.get("dineIn"),
        "reservable": raw_place.get("reservable"),
        "outdoor_seating": raw_place.get("outdoorSeating"),
        "live_music": raw_place.get("liveMusic"),
        "menu_for_children": raw_place.get("menuForChildren"),
        "good_for_children": raw_place.get("goodForChildren"),
        "good_for_groups": raw_place.get("goodForGroups"),
        "allows_dogs": raw_place.get("allowsDogs"),
        "restroom": raw_place.get("restroom"),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main():
    if _LEGACY_OPT_IN not in sys.argv[1:]:
        print(
            "REFUSED: broad/full-mask Places cache is legacy-only. "
            f"Re-run with {_LEGACY_OPT_IN} to acknowledge the quarantine.",
            file=sys.stderr,
        )
        sys.exit(2)
    if sys.stdin.isatty():
        print("REFUSED: legacy cache builder requires JSON on stdin.", file=sys.stderr)
        sys.exit(2)
    # Import the legacy provider helper only after the quarantine acknowledgement.
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from directions import FULL_FIELD_MASK, resolve_places_batched

    input_data = json.load(sys.stdin)
    candidates = input_data["candidates"]
    cache_path = input_data["cache_path"]

    # Load existing cache
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"Loaded existing cache with {len(cache)} entries", file=sys.stderr)

    # Find which queries need resolution (not already cached by maps_query match)
    cached_queries = {v["maps_query"] for v in cache.values()}
    to_resolve = [(i, c) for i, c in enumerate(candidates) if c["maps_query"] not in cached_queries]

    if not to_resolve:
        print("All candidates already cached, nothing to resolve.", file=sys.stderr)
        json.dump(cache, sys.stdout, ensure_ascii=False, indent=2)
        print(file=sys.stdout)
        return

    print(f"Resolving {len(to_resolve)} new places (skipping {len(candidates) - len(to_resolve)} cached)...",
          file=sys.stderr)

    # Batch resolve with full field mask
    queries = [c["maps_query"] for _, c in to_resolve]
    results = resolve_places_batched(queries, field_mask=FULL_FIELD_MASK)

    # Transform and merge into cache
    resolved_count = 0
    failed = []
    for (orig_idx, candidate), result in zip(to_resolve, results):
        if result.get("place_id") is None and result.get("raw") is None:
            failed.append(candidate)
            continue

        raw = result.get("raw", {})
        place_id = raw.get("id")
        if not place_id:
            failed.append(candidate)
            continue

        entry = transform_raw_to_cache(raw, candidate["maps_query"])
        cache[place_id] = entry
        resolved_count += 1
        print(f"  ✓ {candidate['name']}: {entry['display_name']} ({place_id[:20]}...)", file=sys.stderr)

    # Write cache
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {resolved_count} resolved, {len(failed)} failed, {len(cache)} total in cache.",
          file=sys.stderr)

    if failed:
        print("\nFailed to resolve:", file=sys.stderr)
        for c in failed:
            print(f"  ✗ {c['name']}: {c['maps_query']}", file=sys.stderr)

    # Output summary to stdout
    summary = {
        "resolved": resolved_count,
        "failed": len(failed),
        "total_cached": len(cache),
        "failed_names": [c["name"] for c in failed],
        "cache_path": cache_path,
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
