"""
Resolve place IDs and compute routes between places using Google Maps APIs.

Input (stdin JSON):
{
  "places": [{"maps_query": "Place Name, City, Country"}, ...],
  "routes": [{"from": 0, "to": 1}, ...]  // optional, indices into places
  // Per-route departure_time (optional, RFC 3339):
  // "routes": [{"from": 0, "to": 1, "departure_time": "2026-05-18T09:00:00+09:00"}]
}

Output (stdout JSON):
{
  "places": [{"maps_query": ..., "place_id": ..., "lat": ..., "lng": ..., "source": "api"}, ...],
  "routes": [{"from": 0, "to": 1, "modes": {"driving": {"duration_min": N, "distance_km": N}, ...}, "source": "api"}, ...]
}

APIs used:
  - Places API (New):  places.googleapis.com — searchText endpoint
  - Routes API:        routes.googleapis.com — computeRoutes endpoint

Operational limits:
  Quotas are project-specific and may change. Re-check the current Places and
  Routes limits in the active GCP project before every authorized live use.
  The batch defaults below are conservative historical tuning, not current
  quota evidence.

Parallelism strategy:
  - Places:  batch 8 concurrent, 1.0s gap between batches
  - Routes:  batch 15 concurrent, 1.0s gap between batches
  Each individual request within a batch fires simultaneously. Do not raise
  batch sizes without verifying the current project quota and cost boundary.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ROUTES_FIELD_MASK = ",".join([
    "routes.duration",
    "routes.distanceMeters",
    "routes.legs.duration",
    "routes.legs.distanceMeters",
    "routes.legs.startLocation",
    "routes.legs.endLocation",
    "routes.legs.steps.distanceMeters",
    "routes.legs.steps.staticDuration",
    "routes.legs.steps.startLocation",
    "routes.legs.steps.endLocation",
    "routes.legs.steps.travelMode",
    "routes.legs.steps.navigationInstruction",
    "routes.legs.steps.localizedValues",
    "routes.legs.steps.transitDetails",
])

# Internal mode names (backward-compatible with all consumers)
TRANSPORT_MODES = ["driving", "walking", "transit", "bicycling"]

# Mapping: internal mode name → Routes API travelMode enum
MODE_TO_API = {
    "driving": "DRIVE",
    "walking": "WALK",
    "transit": "TRANSIT",
    "bicycling": "BICYCLE",
    "two_wheeler": "TWO_WHEELER",
}

# --- Conservative legacy defaults; not current quota evidence ---
PLACES_BATCH_SIZE = 8
PLACES_BATCH_DELAY = 1.0    # seconds between batches
ROUTES_BATCH_SIZE = 15
ROUTES_BATCH_DELAY = 1.0


DEFAULT_FIELD_MASK = "places.id,places.displayName,places.location"

FULL_FIELD_MASK = (
    "places.id,places.displayName,places.location,places.types,places.primaryType,"
    "places.primaryTypeDisplayName,places.nationalPhoneNumber,places.internationalPhoneNumber,"
    "places.formattedAddress,places.shortFormattedAddress,places.googleMapsUri,places.googleMapsLinks,"
    "places.websiteUri,places.rating,places.userRatingCount,places.priceLevel,places.priceRange,"
    "places.regularOpeningHours,places.currentOpeningHours,places.businessStatus,"
    "places.timeZone,places.utcOffsetMinutes,places.editorialSummary,places.generativeSummary,"
    "places.reviews,places.photos,places.paymentOptions,places.parkingOptions,"
    "places.accessibilityOptions,places.servesBreakfast,places.servesLunch,places.servesDinner,"
    "places.servesBeer,places.servesWine,places.servesBrunch,places.servesVegetarianFood,"
    "places.servesCocktails,places.servesCoffee,places.servesDessert,places.takeout,"
    "places.delivery,places.dineIn,places.reservable,places.outdoorSeating,places.liveMusic,"
    "places.menuForChildren,places.goodForChildren,places.goodForGroups,places.allowsDogs,"
    "places.restroom"
)


def resolve_place(query, max_retries=3, field_mask=None):
    """Resolve a text query to place_id + coordinates via Places API (New).

    When field_mask is provided, returns raw API place data alongside maps_query.
    Default (field_mask=None) returns the simplified dict for backward compat.
    """
    if not API_KEY:
        return {"maps_query": query, "place_id": None, "lat": None, "lng": None, "source": "unavailable"}

    mask = field_mask or DEFAULT_FIELD_MASK

    for attempt in range(max_retries):
        resp = requests.post(
            PLACES_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": API_KEY,
                "X-Goog-FieldMask": mask,
            },
            json={"textQuery": query},
        )
        if resp.status_code in (429, 403) and attempt < max_retries - 1:
            wait = 2 ** (attempt + 1)
            print(f"Rate limited on '{query}', retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break

    data = resp.json()
    candidates = data.get("places", [])
    if not candidates:
        print(f"WARNING: No results for '{query}'", file=sys.stderr)
        return {"maps_query": query, "place_id": None, "lat": None, "lng": None, "source": "not_found"}

    place = candidates[0]

    if field_mask:
        return {"maps_query": query, "raw": place}

    loc = place.get("location", {})
    return {
        "maps_query": query,
        "place_id": place.get("id"),
        "display_name": place.get("displayName", {}).get("text"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "source": "api",
    }


def get_single_route(origin_lat, origin_lng, dest_lat, dest_lng, mode,
                     departure_time=None, max_retries=3):
    """Get route for a single transport mode using Routes API (computeRoutes).

    Args:
        mode: internal mode name (driving, walking, transit, bicycling, two_wheeler)
        departure_time: RFC 3339 string, e.g. "2026-05-18T09:00:00+09:00".
            For transit: affects schedule-based routing (required for accurate results).
            For driving: affects traffic estimates.
            For walking/bicycling: ignored by API.
    Returns:
        (mode, {"duration_min": float, "distance_km": float}) or (mode, None)
    """
    api_mode = MODE_TO_API.get(mode)
    if not api_mode:
        return mode, None

    body = {
        "origin": {
            "location": {
                "latLng": {"latitude": origin_lat, "longitude": origin_lng}
            }
        },
        "destination": {
            "location": {
                "latLng": {"latitude": dest_lat, "longitude": dest_lng}
            }
        },
        "travelMode": api_mode,
    }

    if departure_time:
        body["departureTime"] = departure_time

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": ROUTES_FIELD_MASK,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(ROUTES_URL, json=body, headers=headers)
            if resp.status_code in (429, 403) and attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited on routes {mode}, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            routes = data.get("routes", [])
            if routes:
                route = routes[0]
                duration_str = route.get("duration", "0s")
                duration_sec = int(duration_str.rstrip("s"))
                distance_m = route.get("distanceMeters", 0)
                result = {
                    "duration_min": round(duration_sec / 60, 1),
                    "distance_km": round(distance_m / 1000, 1),
                }
                # Store full leg/step details (transit stops, lines, schedules, etc.)
                legs = route.get("legs", [])
                if legs:
                    result["legs"] = legs
                return mode, result
            return mode, None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"WARNING: Routes API error for mode={mode}: {e}", file=sys.stderr)
            return mode, None
    return mode, None


# Backward-compatible alias
get_single_direction = get_single_route


def get_directions(origin_lat, origin_lng, dest_lat, dest_lng, departure_time=None,
                   country_code=None, available_modes=None):
    """Get routes for all transport modes between two coordinates (parallel).

    Args:
        departure_time: optional RFC 3339 string. Only passed to transit mode
            (schedule-based routing). Driving with departure_time triggers
            traffic-aware routing which requires a higher billing tier.
        country_code: optional ISO 3166-1 alpha-2 code (e.g. "JP", "TW").
            When provided, skips modes known to be unsupported in that country
            (saves API calls and avoids empty results). See routes_coverage.py.
        available_modes: optional list of mode names to query (e.g. ["walking", "transit"]).
            When provided, ONLY these modes are queried — saves API calls when
            the user has no access to certain transport (e.g. no car, no scooter).
    """
    if not API_KEY:
        return {"modes": {}, "source": "unavailable"}

    # Determine which modes to query
    modes_to_query = list(available_modes) if available_modes else list(TRANSPORT_MODES)
    skipped_modes = []
    if country_code:
        try:
            from routes_coverage import get_supported_modes
            coverage = get_supported_modes(country_code)
            skipped_modes = [m for m in modes_to_query if not coverage["modes"].get(m, True)]
            modes_to_query = [m for m in modes_to_query if m not in skipped_modes]
            if skipped_modes:
                print(f"Skipping unsupported modes for {country_code}: {skipped_modes}",
                      file=sys.stderr)
        except ImportError:
            pass  # routes_coverage.py not available, query all modes

    modes = {}
    with ThreadPoolExecutor(max_workers=max(len(modes_to_query), 1)) as pool:
        futures = {}
        for mode in modes_to_query:
            # Only pass departure_time for transit (schedule-dependent).
            # Other modes with departure_time trigger higher billing tiers.
            dep = departure_time if mode == "transit" else None
            fut = pool.submit(get_single_route, origin_lat, origin_lng, dest_lat, dest_lng,
                              mode, dep)
            futures[fut] = mode

        for f in as_completed(futures):
            mode, result = f.result()
            if result:
                modes[mode] = result

    result = {"modes": modes, "source": "api"}
    if skipped_modes:
        result["skipped_modes"] = skipped_modes
    return result


def batched(items, batch_size):
    """Yield successive batches from items list."""
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def resolve_places_batched(queries, field_mask=None):
    """Resolve all place queries using batched parallelism."""
    results = [None] * len(queries)

    for batch_idx, batch in enumerate(batched(list(enumerate(queries)), PLACES_BATCH_SIZE)):
        if batch_idx > 0:
            time.sleep(PLACES_BATCH_DELAY)

        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {
                pool.submit(resolve_place, query, 3, field_mask): idx
                for idx, query in batch
            }
            for f in as_completed(futures):
                idx = futures[f]
                results[idx] = f.result()

    return results


def compute_routes_batched(route_specs, available_modes=None):
    """Compute all routes using batched parallelism.

    route_specs: list of (from_idx, to_idx, origin_lat, origin_lng, dest_lat, dest_lng)
                 or      (from_idx, to_idx, origin_lat, origin_lng, dest_lat, dest_lng, departure_time)
    available_modes: optional list of mode names to query (passed to get_directions)
    """
    results = [None] * len(route_specs)

    for batch_idx, batch in enumerate(batched(list(enumerate(route_specs)), ROUTES_BATCH_SIZE)):
        if batch_idx > 0:
            time.sleep(ROUTES_BATCH_DELAY)

        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for list_idx, spec in batch:
                fr, to, olat, olng, dlat, dlng = spec[:6]
                dep_time = spec[6] if len(spec) > 6 else None
                fut = pool.submit(get_directions, olat, olng, dlat, dlng, dep_time,
                                  available_modes=available_modes)
                futures[fut] = (list_idx, fr, to)

            for f in as_completed(futures):
                list_idx, fr, to = futures[f]
                route_result = f.result()
                results[list_idx] = {"from": fr, "to": to, **route_result}

    return results


def main():
    t0 = time.time()
    input_data = json.load(sys.stdin)

    # Resolve places (batched parallel).
    # Places with "lat" and "lng" already set are pre-resolved — skip API call.
    raw_places = input_data.get("places", [])
    needs_resolution = []  # (index, query) pairs
    places = [None] * len(raw_places)

    for i, p in enumerate(raw_places):
        if p.get("lat") and p.get("lng"):
            # Pre-resolved: pass through as-is
            places[i] = {"maps_query": p.get("maps_query", ""), "lat": p["lat"], "lng": p["lng"],
                         "place_id": p.get("place_id"), "display_name": p.get("display_name")}
        else:
            needs_resolution.append((i, p["maps_query"]))

    t1 = time.time()
    if needs_resolution:
        queries = [q for _, q in needs_resolution]
        resolved = resolve_places_batched(queries)
        for (idx, _), result in zip(needs_resolution, resolved):
            places[idx] = result
    t2 = time.time()
    n_pre = len(raw_places) - len(needs_resolution)
    print(f"Places: {n_pre} pre-resolved, {len(needs_resolution)} API-resolved in {t2-t1:.1f}s", file=sys.stderr)

    # Compute routes (batched parallel)
    route_specs = []
    for r in input_data.get("routes", []):
        fr, to = r["from"], r["to"]
        origin, dest = places[fr], places[to]
        dep_time = r.get("departure_time")
        if origin.get("lat") and dest.get("lat"):
            spec = (fr, to, origin["lat"], origin["lng"], dest["lat"], dest["lng"])
            if dep_time:
                spec = spec + (dep_time,)
            route_specs.append(spec)
        else:
            route_specs.append(None)

    # Read available_modes from input (limits which modes are queried)
    avail_modes = input_data.get("available_modes")
    if avail_modes:
        print(f"Querying only modes: {avail_modes}", file=sys.stderr)

    valid_specs = [(i, s) for i, s in enumerate(route_specs) if s is not None]
    t3 = time.time()
    if valid_specs:
        route_results = compute_routes_batched([s for _, s in valid_specs], available_modes=avail_modes)
        routes = [None] * len(route_specs)
        for result, (orig_idx, _) in zip(route_results, valid_specs):
            routes[orig_idx] = result
        # Fill unavailable routes
        for i, spec in enumerate(route_specs):
            if spec is None:
                fr, to = input_data["routes"][i]["from"], input_data["routes"][i]["to"]
                routes[i] = {"from": fr, "to": to, "modes": {}, "source": "unavailable"}
    else:
        routes = []
    t4 = time.time()
    print(f"Routes: {len(routes)} computed in {t4-t3:.1f}s", file=sys.stderr)
    print(f"Total: {t4-t0:.1f}s", file=sys.stderr)

    output = {"places": places, "routes": routes}
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
