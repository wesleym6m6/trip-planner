"""
Render a trip's HTML page from template + JSON data.

Usage: python3 scripts/render_trip.py trips/vietnam-2026-05
Output: trips/vietnam-2026-05/index.html
"""
import json
import sys
import pathlib
from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = pathlib.Path(__file__).parent.parent / "template"
EMOJI_MAP = {
    "flight": "✈️", "hotel": "🏨", "work": "💻",
    "food": "🍜", "spot": "📍", "drink": "☕",
    "transport": "🚊", "flight": "✈️",
}
COLOR_MAP = {
    "hotel": "#e74c3c", "work": "#2e86c1", "food": "#e67e22",
    "spot": "#27ae60", "drink": "#8e44ad",
    "transport": "#95a5a6", "flight": "#95a5a6",
}
MODE_ICON = {
    "driving": "🚗", "walking": "🚶", "transit": "🚇", "bicycling": "🛵",
}


def load_json(path):
    return json.loads(path.read_text())


def load_json_optional(path):
    if path.exists():
        return json.loads(path.read_text())
    return []


def main():
    trip_dir = pathlib.Path(sys.argv[1])

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from validate_trip import validate
    errors = validate(trip_dir)
    if errors:
        print(f"Validation failed ({len(errors)} error(s)):", file=sys.stderr)
        for e in errors:
            print(f"  \u2717 {e}", file=sys.stderr)
        sys.exit(1)

    data_dir = trip_dir / "data"

    from plan_compat import load_trip_views

    trip, itinerary, _trip_id, _revision = load_trip_views(data_dir)
    info = load_json(data_dir / "info.json")
    reservations = load_json_optional(data_dir / "reservations.json")
    packing = load_json_optional(data_dir / "packing.json")
    todo = load_json_optional(data_dir / "todo.json")

    slug = trip.get("slug", trip_dir.name)

    # Build map points for Leaflet
    map_points = []
    for day in itinerary["days"]:
        for place in day["places"]:
            if place.get("lat") and place.get("lng"):
                map_points.append({
                    "n": place["title"],
                    "lat": place["lat"],
                    "lng": place["lng"],
                    "d": f"d{day['day']}",
                    "c": place["type"],
                    "desc": place.get("note", ""),
                    "place_id": place.get("place_id"),
                    "maps_query": place.get("maps_query", ""),
                })

    # Detect UTC offset from places_cache for local time display
    cache_path = data_dir / "places_cache.json"
    utc_offset_min = 0
    if cache_path.exists():
        cache = load_json(cache_path)
        for pid, p in cache.items():
            offset = p.get("utc_offset_minutes")
            if offset is not None:
                utc_offset_min = offset
                break

    # Pre-process transit steps: convert UTC times to local time strings
    local_tz = timezone(timedelta(minutes=utc_offset_min))
    for day in itinerary["days"]:
        for t in day.get("travel", []):
            transit = t.get("modes", {}).get("transit", {})
            for step in transit.get("transit_steps", []):
                stops = step.get("stopDetails", {})
                for key in ("departureTime", "arrivalTime"):
                    utc_str = stops.get(key)
                    if utc_str and utc_str.endswith("Z"):
                        try:
                            utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
                            local_dt = utc_dt.astimezone(local_tz)
                            stops[key + "Local"] = local_dt.strftime("%H:%M")
                        except (ValueError, TypeError):
                            pass

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("trip.html")

    html = template.render(
        trip=trip,
        itinerary=itinerary,
        info=info,
        reservations=reservations,
        packing=packing,
        todo=todo,
        slug=slug,
        map_points=map_points,
        emoji_map=EMOJI_MAP,
        color_map=COLOR_MAP,
        mode_icon=MODE_ICON,
    )

    output = trip_dir / "index.html"
    output.write_text(html)
    print(f"Rendered: {output}")

    # Generate ICS calendar file
    from generate_ics import generate_ics
    generate_ics(trip_dir)


if __name__ == "__main__":
    main()
