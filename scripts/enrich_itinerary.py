"""
One-time script: enrich itinerary.json with place_id, coordinates, and travel data.
Reads itinerary.json, calls directions.py (Routes API), writes back enriched data.
Also auto-selects recommended_mode for each travel segment based on distance.

Usage:
  direnv exec $REPO python3 scripts/enrich_itinerary.py <itinerary.json> [modes] [timezone]

  modes:    comma-separated available modes (e.g. walking,transit,driving)
  timezone: UTC offset for departure_time (e.g. +09:00 for Japan, +08:00 for Taiwan)
            When provided, transit queries use actual scheduled departure times
            from the itinerary (day.date + place.time + timezone).
            Without timezone, transit queries use no departure_time (less accurate).

Examples:
  # Synthetic trip without transit:
  direnv exec $REPO python3 scripts/enrich_itinerary.py trips/{slug}/data/itinerary.json walking,driving

  # Synthetic trip with transit and an explicit timezone:
  direnv exec $REPO python3 scripts/enrich_itinerary.py trips/{slug}/data/itinerary.json walking,transit,driving +08:00

  # Synthetic trip with two-wheeler support:
  direnv exec $REPO python3 scripts/enrich_itinerary.py trips/{slug}/data/itinerary.json walking,transit,two_wheeler +07:00
"""
import json
import subprocess
import sys
import pathlib
import re

if __package__:
    from .plan_compat import (
        CanonicalWriteRefused,
        refuse_canonical_write,
        resolve_ordered_local_datetimes,
    )
else:
    from plan_compat import (
        CanonicalWriteRefused,
        refuse_canonical_write,
        resolve_ordered_local_datetimes,
    )


# Distance-based mode selection thresholds (km)
WALK_MAX_KM = 1.0       # <= 1 km: recommend walking
SCOOTER_MAX_KM = 5.0    # 1-5 km: recommend bicycling (scooter proxy)
                         # > 5 km: recommend driving (Grab/taxi)

# Routes API supports TWO_WHEELER mode for real scooter routing (Enterprise tier).
# For backward compatibility, "bicycling" is still available as a cheaper proxy:
# - bicycling speed (~12 km/h) is ~2x slower than scooter (~25 km/h)
# - SCOOTER_SPEED_FACTOR corrects bicycling durations when used as scooter proxy
# To use real scooter routing, add "two_wheeler" to available_modes instead.
SCOOTER_SPEED_FACTOR = 0.5  # bicycling_time * 0.5 ≈ scooter_time


def format_departure_time(
    day_date: object, local_time_text: object, utc_offset: object
) -> str | None:
    """Build RFC 3339 departure text from a lossless local plan time."""

    if not all(
        isinstance(value, str) and value
        for value in (day_date, local_time_text, utc_offset)
    ):
        return None
    resolved = resolve_ordered_local_datetimes(
        day_date, (local_time_text,)
    )[0]
    if resolved is None:
        return None
    return f"{resolved.isoformat()}{utc_offset}"


def select_recommended_mode(modes, available_modes=None):
    """Select the best transport mode based on distance and availability.

    available_modes: set/list of modes the user can actually use, e.g.
      {"walking", "driving"}           — no scooter, no transit
      {"walking", "bicycling"}         — scooter + walking only
      {"walking", "driving", "transit"} — public transport city
      None                             — all modes available (default)

    Returns one of: 'walking', 'bicycling', 'driving', 'transit', or None.
    """
    if not modes:
        return None

    # Filter to available modes
    if available_modes is not None:
        avail = set(available_modes)
    else:
        avail = {"walking", "bicycling", "driving", "transit"}

    # Only consider modes that are both available AND have API data
    usable = {m for m in avail if m in modes}
    if not usable:
        return None

    # Get driving distance as the reference (most reliable for distance)
    distance_km = None
    for m in ["driving", "walking", "bicycling"]:
        if m in modes and modes[m].get("distance_km"):
            distance_km = modes[m]["distance_km"]
            break

    if distance_km is None:
        # No distance data, pick first available
        for m in ["driving", "bicycling", "transit", "walking"]:
            if m in usable:
                return m
        return None

    if distance_km <= WALK_MAX_KM and "walking" in usable:
        return "walking"
    if distance_km <= SCOOTER_MAX_KM and "bicycling" in usable:
        return "bicycling"
    if "driving" in usable:
        return "driving"
    if "transit" in usable:
        return "transit"
    if "bicycling" in usable:
        return "bicycling"
    if "walking" in usable:
        return "walking"
    return None


def _usage() -> str:
    return (
        "Usage: python3 scripts/enrich_itinerary.py "
        "<itinerary.json> [walking,driving] [+09:00]"
    )


def main():
    if len(sys.argv) < 2:
        print(_usage(), file=sys.stderr)
        sys.exit(2)
    itinerary_path = pathlib.Path(sys.argv[1])
    try:
        refuse_canonical_write(
            itinerary_path.parent, operation="enrich_itinerary.py"
        )
    except CanonicalWriteRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    itinerary = json.loads(itinerary_path.read_text())

    # Parse available_modes from CLI arg or itinerary metadata
    # Usage: python3 enrich_itinerary.py itinerary.json [walking,driving] [+09:00]
    available_modes = None
    utc_offset = None

    for arg in sys.argv[2:]:
        if re.match(r'^[+-]\d{2}:\d{2}$', arg):
            utc_offset = arg
        elif ',' in arg or arg in ("walking", "driving", "transit", "bicycling", "two_wheeler"):
            available_modes = set(arg.split(","))

    if available_modes is None and "available_modes" in itinerary:
        available_modes = set(itinerary["available_modes"])

    if available_modes:
        print(f"Available modes: {available_modes}", file=sys.stderr)
    if utc_offset:
        print(f"Timezone offset: {utc_offset} (transit will use scheduled departure times)", file=sys.stderr)
    elif available_modes and "transit" in available_modes:
        print("WARNING: transit mode without timezone — queries will not use scheduled departure times", file=sys.stderr)

    # Collect all places and routes.
    # Places that already have lat/lng are "pre-resolved" — they skip API
    # resolution and their place_id/lat/lng/display_name are preserved.
    # This prevents manual cache entries (Google Maps 未收錄) from being
    # overwritten by a wrong API result, and avoids 400 errors from entries
    # with no maps_query (e.g. transport waypoints).
    all_places = []
    all_routes = []
    pre_resolved = set()  # global indices of pre-resolved places
    place_index_map = {}  # (day_idx, place_idx) -> global index

    for day_idx, day in enumerate(itinerary["days"]):
        for place_idx, place in enumerate(day["places"]):
            global_idx = len(all_places)
            place_index_map[(day_idx, place_idx)] = global_idx

            has_coords = place.get("lat") and place.get("lng")
            if has_coords:
                # Pre-resolved: pass lat/lng directly so directions.py
                # skips API resolution and uses these for route computation.
                pre_resolved.add(global_idx)
                all_places.append({
                    "maps_query": place.get("maps_query", ""),
                    "lat": place["lat"],
                    "lng": place["lng"],
                    "place_id": place.get("place_id"),
                    "display_name": place.get("display_name"),
                })
            else:
                all_places.append({"maps_query": place.get("maps_query", "")})

        # Routes between consecutive places within a day
        day_date = day.get("date")  # e.g. "2026-05-18"
        resolved_times = resolve_ordered_local_datetimes(
            day_date,
            (place.get("time") for place in day["places"]),
            available_start=day.get("available_start"),
            available_end=day.get("available_end"),
        )
        for i in range(len(day["places"]) - 1):
            route_entry = {
                "from": place_index_map[(day_idx, i)],
                "to": place_index_map[(day_idx, i + 1)],
            }
            # Build departure_time from the "from" place's scheduled time.
            # This tells the Routes API: "I'm leaving place A at this time,
            # what transit should I take to reach place B?"
            if utc_offset and resolved_times[i] is not None:
                route_entry["departure_time"] = (
                    f"{resolved_times[i].isoformat()}{utc_offset}"
                )
            all_routes.append(route_entry)

    n_pre = len(pre_resolved)
    n_new = len(all_places) - n_pre
    print(f"Places: {n_pre} pre-resolved (have lat/lng), {n_new} need API resolution", file=sys.stderr)

    # Call directions.py — pass available_modes so it only queries needed modes
    input_data = {"places": all_places, "routes": all_routes}
    if available_modes:
        input_data["available_modes"] = sorted(available_modes)
    result = subprocess.run(
        [sys.executable, "scripts/directions.py"],
        input=json.dumps(input_data),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"directions.py failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    resolved = json.loads(result.stdout)

    # Write back to itinerary — skip pre-resolved entries
    global_idx = 0
    for day in itinerary["days"]:
        for place in day["places"]:
            if global_idx not in pre_resolved:
                r = resolved["places"][global_idx]
                place["place_id"] = r.get("place_id")
                place["lat"] = r.get("lat")
                place["lng"] = r.get("lng")
                if r.get("display_name"):
                    place["display_name"] = r["display_name"]
            global_idx += 1

    # Map routes back to days
    route_idx = 0
    for day in itinerary["days"]:
        if "travel" not in day:
            day["travel"] = []

        for i in range(len(day["places"]) - 1):
            route = resolved["routes"][route_idx]
            # Find or create travel entry for this pair
            modes = route.get("modes", {})
            recommended = select_recommended_mode(modes, available_modes)

            # Build travel entry with core fields
            travel_data = {
                "from": i,
                "to": i + 1,
                "modes": {m: {"duration_min": v["duration_min"], "distance_km": v["distance_km"]}
                          for m, v in modes.items()},
                "source": route.get("source", "api"),
                "recommended_mode": recommended,
            }

            # Store full transit leg details (stops, lines, schedules) when available.
            # These are stored per-mode alongside duration/distance.
            for m, v in modes.items():
                if "legs" in v:
                    # Extract transit steps only (WALK steps are noise for storage)
                    transit_steps = []
                    for leg in v["legs"]:
                        for step in leg.get("steps", []):
                            if step.get("travelMode") == "TRANSIT" and "transitDetails" in step:
                                transit_steps.append(step["transitDetails"])
                    if transit_steps:
                        travel_data["modes"][m]["transit_steps"] = transit_steps

            existing = next((t for t in day["travel"] if t["from"] == i and t["to"] == i + 1), None)
            if existing:
                existing.update(travel_data)
            else:
                day["travel"].append(travel_data)
            route_idx += 1

    # Apply scooter speed correction to bicycling durations
    for day in itinerary["days"]:
        for t in day.get("travel", []):
            if "bicycling" in t.get("modes", {}):
                raw = t["modes"]["bicycling"].get("duration_min", 0)
                t["modes"]["bicycling"]["duration_min"] = round(raw * SCOOTER_SPEED_FACTOR, 1)

    itinerary_path.write_text(json.dumps(itinerary, ensure_ascii=False, indent=2) + "\n")

    # Print summary
    for day in itinerary["days"]:
        for t in day.get("travel", []):
            rm = t.get("recommended_mode")
            m = t.get("modes", {}).get(rm, {}) if rm else {}
            dur = m.get("duration_min", "?")
            dist = m.get("distance_km", "?")
            print(f"  Day {day['day']} [{t['from']}→{t['to']}] {rm}: {dur} min, {dist} km")
    print(f"Enriched {global_idx} places and {route_idx} routes.")


if __name__ == "__main__":
    main()
