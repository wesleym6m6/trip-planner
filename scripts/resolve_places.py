"""
Resolve candidate places and compute a distance matrix for trip planning.

Used by the trip-planner skill BEFORE writing itinerary.json, so the AI can
see actual distances and group nearby places on the same day.

Input (stdin JSON):
{
  "places": [
    {"name": "Example Museum", "maps_query": "Example Museum, Example City"},
    {"name": "Example Park", "maps_query": "Example Park, Example City"},
    ...
  ]
}

Output (stdout JSON):
{
  "places": [
    {"name": "Example Museum", "maps_query": "...", "place_id": "ChIJ...", "lat": 1.0, "lng": 1.0},
    ...
  ],
  "distance_matrix": [
    [0.0, 3.2, 1.5, ...],   // km from place 0 to each other place
    [3.2, 0.0, 2.1, ...],
    ...
  ],
  "clusters": [
    {"center": "Example Museum", "nearby": ["Example Park", "Example Cafe"], "note": "< 1 km apart"},
    ...
  ]
}

Usage:
  echo '{"places": [...]}' | direnv exec $REPO python3 scripts/resolve_places.py
"""
import json
import math
import sys

# Import resolve logic from directions.py
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from directions import resolve_places_batched


def haversine_km(lat1, lng1, lat2, lng2):
    """Compute straight-line distance between two coordinates in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_clusters(places, matrix, threshold_km=1.5):
    """Group places that are within threshold_km of each other."""
    n = len(places)
    visited = set()
    clusters = []

    for i in range(n):
        if i in visited or not places[i].get("lat"):
            continue
        group = [i]
        for j in range(i + 1, n):
            if j in visited or not places[j].get("lat"):
                continue
            if matrix[i][j] <= threshold_km:
                group.append(j)

        if len(group) > 1:
            visited.update(group)
            center = places[group[0]]["name"]
            nearby = [places[g]["name"] for g in group[1:]]
            max_dist = max(matrix[group[0]][g] for g in group[1:])
            clusters.append({
                "center": center,
                "nearby": nearby,
                "note": f"< {max_dist:.1f} km apart",
                "indices": group,
            })

    return clusters


def main():
    input_data = json.load(sys.stdin)
    place_inputs = input_data.get("places", [])

    if not place_inputs:
        json.dump({"places": [], "distance_matrix": [], "clusters": []}, sys.stdout, ensure_ascii=False, indent=2)
        return

    # Resolve all places
    queries = [p["maps_query"] for p in place_inputs]
    resolved = resolve_places_batched(queries)

    # Merge names back
    places = []
    for i, r in enumerate(resolved):
        places.append({
            "name": place_inputs[i]["name"],
            **r,
        })

    # Compute distance matrix
    n = len(places)
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if places[i].get("lat") and places[j].get("lat"):
                d = round(haversine_km(places[i]["lat"], places[i]["lng"], places[j]["lat"], places[j]["lng"]), 2)
            else:
                d = -1  # unknown
            matrix[i][j] = d
            matrix[j][i] = d

    # Find clusters
    clusters = find_clusters(places, matrix)

    output = {
        "places": places,
        "distance_matrix": matrix,
        "clusters": clusters,
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
