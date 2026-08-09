"""
Score a given route — calculate total time and distance without optimization.

Use when the user or AI has a specific route in mind and just wants to know
how long it takes. No optimization, no rearranging — just measurement.

All values below are synthetic schema examples, not route evidence.

Input (stdin JSON):
{
  "route": ["Example Museum", "Example Cafe", "Example Park"],
  "maps_queries": {
    "Example Museum": "Example Museum, Example City",
    "Example Cafe": "Example Cafe, Example City",
    ...
  },
  "modes": ["walking", "driving", "bicycling"]  // optional, default all 4
}

Or use coordinates directly:
{
  "route": [
    {"name": "Example Museum", "lat": 1.0001, "lng": 1.0002},
    {"name": "Example Cafe", "lat": 1.0003, "lng": 1.0004},
    ...
  ]
}

Output (stdout JSON):
{
  "segments": [
    {
      "from": "Example Museum",
      "to": "Example Cafe",
      "distance_km": {"walking": 0.8, "driving": 1.1, "bicycling": 0.9},
      "duration_min": {"walking": 12, "driving": 5, "bicycling": 4},
      "recommended": "walking"
    },
    ...
  ],
  "totals": {
    "walking":   {"distance_km": 8.2, "duration_min": 95},
    "driving":   {"distance_km": 9.5, "duration_min": 28},
    "bicycling": {"distance_km": 8.8, "duration_min": 22}
  },
  "recommended_total": {"distance_km": 7.5, "duration_min": 35, "modes_used": "2× walking, 3× driving"},
  "compute_time_ms": 3200
}

Usage:
  echo '{"route": [...], "maps_queries": {...}}' | \
    direnv exec $REPO python3 scripts/score_route.py
"""
import json
import os
import sys
import time

# Import from directions.py
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from directions import resolve_places_batched, get_directions

# Import mode selection from enrich
from enrich_itinerary import select_recommended_mode

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")


def main():
    t0 = time.time()
    input_data = json.load(sys.stdin)

    route = input_data["route"]
    requested_modes = input_data.get("modes", ["driving", "walking", "transit", "bicycling"])
    # available_modes: which modes the user can actually use (affects recommendation)
    # If not specified, all requested_modes are available
    available_modes = input_data.get("available_modes")
    if available_modes is not None:
        available_modes = set(available_modes)

    # Resolve coordinates if needed
    places = []
    if route and isinstance(route[0], str):
        # Names provided, need maps_queries to resolve
        maps_queries = input_data.get("maps_queries", {})
        queries = []
        for name in route:
            q = maps_queries.get(name)
            if not q:
                print(f"ERROR: no maps_query for '{name}'", file=sys.stderr)
                sys.exit(1)
            queries.append(q)

        resolved = resolve_places_batched(queries)
        for i, r in enumerate(resolved):
            places.append({"name": route[i], "lat": r.get("lat"), "lng": r.get("lng")})
    else:
        # Coordinates provided directly
        places = route

    # Validate
    for p in places:
        if not p.get("lat") or not p.get("lng"):
            print(f"ERROR: missing coordinates for '{p.get('name', '?')}'", file=sys.stderr)
            sys.exit(1)

    if len(places) < 2:
        print("ERROR: need at least 2 places", file=sys.stderr)
        sys.exit(1)

    # Get directions for each consecutive pair
    segments = []
    mode_totals = {m: {"distance_km": 0, "duration_min": 0} for m in requested_modes}

    for i in range(len(places) - 1):
        fr, to = places[i], places[i + 1]
        directions = get_directions(fr["lat"], fr["lng"], to["lat"], to["lng"])
        modes = directions.get("modes", {})

        seg_dist = {}
        seg_dur = {}
        for m in requested_modes:
            if m in modes:
                seg_dist[m] = modes[m]["distance_km"]
                seg_dur[m] = modes[m]["duration_min"]
                mode_totals[m]["distance_km"] += modes[m]["distance_km"]
                mode_totals[m]["duration_min"] += modes[m]["duration_min"]

        recommended = select_recommended_mode(modes, available_modes)

        segments.append({
            "from": fr["name"],
            "to": to["name"],
            "distance_km": seg_dist,
            "duration_min": seg_dur,
            "recommended": recommended,
        })

    # Build recommended total
    rec_dist = 0
    rec_dur = 0
    mode_counts = {}
    for seg in segments:
        rm = seg["recommended"]
        if rm and rm in seg["duration_min"]:
            rec_dist += seg["distance_km"].get(rm, 0)
            rec_dur += seg["duration_min"].get(rm, 0)
            mode_counts[rm] = mode_counts.get(rm, 0) + 1

    modes_used = ", ".join(f"{v}× {k}" for k, v in sorted(mode_counts.items(), key=lambda x: -x[1]))

    # Round totals
    for m in mode_totals:
        mode_totals[m]["distance_km"] = round(mode_totals[m]["distance_km"], 1)
        mode_totals[m]["duration_min"] = round(mode_totals[m]["duration_min"], 1)

    elapsed_ms = round((time.time() - t0) * 1000)

    output = {
        "segments": segments,
        "totals": mode_totals,
        "recommended_total": {
            "distance_km": round(rec_dist, 1),
            "duration_min": round(rec_dur, 1),
            "modes_used": modes_used,
        },
        "compute_time_ms": elapsed_ms,
    }

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
