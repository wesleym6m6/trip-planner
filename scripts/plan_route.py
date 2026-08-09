"""
Find efficient day-assignments and orderings for a set of places.

This is a TOOL for AI, not a decision-maker. It returns the top N arrangements
ranked by total travel distance. AI then applies soft constraints (meal timing,
vibe, opening hours) to pick or adjust.

Input (stdin JSON):
{
  "places": [
    {"name": "Example Museum", "lat": 1.0001, "lng": 1.0002, "type": "spot"},
    {"name": "Example Cafe", "lat": 1.0003, "lng": 1.0004, "type": "food"},
    ...
  ],
  "days": 3,
  "start": "Example Hotel",     // optional: name of daily start point
  "fixed": {"Example Park": 2}, // optional: place must be on this day (1-indexed)
  "per_day_min": 2,             // optional, default 2
  "per_day_max": 6,             // optional, default 6
  "top_n": 5,                   // optional, default 5
  "iterations": 5000,           // optional, SA iterations per restart, default 5000
  "restarts": 5,                // optional, default 5
  "ai_solution": {              // optional: AI's own arrangement to score
    "1": ["Example Hotel","Example Museum","Example Cafe"],
    "2": ["Example Hotel","Example Park","Example Market"],
    ...
  }
}

Output (stdout JSON):
{
  "solutions": [
    {
      "rank": 1,
      "score_km": 12.3,
      "days": {
        "1": ["Example Museum", "Example Cafe", "Example Store"],
        "2": ["Example Park", "Example Market", "Example District"],
        ...
      },
      "day_details": [
        {"day": 1, "count": 3, "intra_km": 1.5},
        ...
      ]
    },
    ...
  ],
  "ai_comparison": {            // only if ai_solution provided
    "score_km": 15.1,
    "rank_among_top_n": 8,      // where it would rank (> top_n = worse than all)
    "delta_vs_best_pct": 22.8,  // % worse than best found
    "verdict": "acceptable"     // "optimal" (<5%), "acceptable" (5-30%), "inefficient" (>30%)
  },
  "compute_time_ms": 1243,
  "config": {"places": 50, "days": 10, "iterations": 5000, "restarts": 5}
}

Algorithm: Simulated Annealing with random restarts.
- K-means-seeded initial assignment (maximally spread seeds)
- Swap + move mutations with temperature-based acceptance
- Nearest-neighbor TSP for intra-day ordering (O(n²) per day)
- 5000 iterations × 5 restarts ≈ 1-2 seconds for 50 places / 10 days
"""
import copy
import json
import math
import random
import sys
import time


def haversine_km(lat1, lng1, lat2, lng2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_distance_matrix(places):
    n = len(places)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_km(
                places[i]["lat"], places[i]["lng"],
                places[j]["lat"], places[j]["lng"],
            )
            dist[i][j] = d
            dist[j][i] = d
    return dist


def nn_tsp_cost(indices, dist, pos_constraints=None):
    """Nearest-neighbor TSP with position constraints.

    pos_constraints: dict of {index: position} where position is 0-indexed int
    or -1 for last. Only indices in this day are relevant.
    Returns (ordered_indices, total_km).
    """
    if len(indices) < 2:
        return list(indices), 0.0

    pc = pos_constraints or {}
    # Separate pinned and free indices
    pinned = {}  # position -> index
    last_pinned = None
    free = []
    for idx in indices:
        if idx in pc:
            p = pc[idx]
            if p == -1:
                last_pinned = idx
            else:
                pinned[p] = idx
        else:
            free.append(idx)

    # Build order: fill pinned positions, NN for free slots
    n = len(indices)
    order = [None] * n

    # Place pinned-position items
    for pos, idx in pinned.items():
        if 0 <= pos < n:
            order[pos] = idx

    # Place last-pinned item
    if last_pinned is not None:
        order[n - 1] = last_pinned

    # Fill remaining slots with NN among free items
    remaining = set(free)
    for i in range(n):
        if order[i] is not None:
            continue
        if not remaining:
            break
        # Find nearest to previous filled position
        prev = None
        for j in range(i - 1, -1, -1):
            if order[j] is not None:
                prev = order[j]
                break
        if prev is not None:
            nxt = min(remaining, key=lambda p: dist[prev][p])
        else:
            nxt = remaining.pop()
            remaining.add(nxt)
            nxt = min(remaining, key=lambda p: dist[p][p])  # arbitrary
        order[i] = nxt
        remaining.discard(nxt)

    # Remove any None slots (shouldn't happen but safety)
    order = [x for x in order if x is not None]

    # Compute total distance
    total = sum(dist[order[i]][order[i + 1]] for i in range(len(order) - 1))
    return order, total


def total_cost(days, dist, all_pos_constraints=None):
    """Total cost across all days, respecting position constraints."""
    apc = all_pos_constraints or {}
    return sum(nn_tsp_cost(d, dist, apc.get(di))[1] for di, d in enumerate(days) if d)


def kmeans_init(n, d, dist, per_day_min, per_day_max, fixed, seed=0):
    """K-means-ish initial assignment with constraints."""
    random.seed(seed)

    # Pick D seeds maximally spread
    seeds = [random.randint(0, n - 1)]
    for _ in range(d - 1):
        candidates = [p for p in range(n) if p not in seeds]
        if not candidates:
            break
        farthest = max(candidates, key=lambda p: min(dist[p][s] for s in seeds))
        seeds.append(farthest)

    days = [[] for _ in range(d)]

    # Place fixed assignments first
    placed = set()
    for idx, day_num in fixed.items():
        days[day_num].append(idx)
        placed.add(idx)

    # Assign rest to nearest seed
    unplaced = [p for p in range(n) if p not in placed]
    random.shuffle(unplaced)
    for p in unplaced:
        best_d = min(range(d), key=lambda di: dist[p][seeds[di]] if di < len(seeds) else float("inf"))
        if len(days[best_d]) < per_day_max:
            days[best_d].append(p)
        else:
            for di in sorted(range(d), key=lambda di: dist[p][seeds[di]] if di < len(seeds) else float("inf")):
                if len(days[di]) < per_day_max:
                    days[di].append(p)
                    break

    return days


def is_valid(days, per_day_min, per_day_max, fixed):
    """Check all constraints."""
    for di, day in enumerate(days):
        if len(day) < per_day_min or len(day) > per_day_max:
            return False
    for idx, day_num in fixed.items():
        if idx not in days[day_num]:
            return False
    return True


def sa_optimize(n, d, dist, per_day_min, per_day_max, fixed, pos_constraints, iterations, seed=0):
    """Simulated annealing optimization.

    fixed: {place_idx: day_idx} — place must be on this day
    pos_constraints: {day_idx: {place_idx: position}} — place must be at this position in day
    """
    days = kmeans_init(n, d, dist, per_day_min, per_day_max, fixed, seed)
    # Set of indices that have position constraints (cannot be moved between days)
    pos_locked = set()
    for day_pc in pos_constraints.values():
        pos_locked.update(day_pc.keys())

    cost = total_cost(days, dist, pos_constraints)
    best_days = copy.deepcopy(days)
    best_cost = cost

    for it in range(iterations):
        new_days = copy.deepcopy(days)
        temp = max(0.01, 1.0 - it / iterations)

        op = random.random()
        if op < 0.5:
            # Swap two places between different days
            d1, d2 = random.sample(range(d), 2)
            if new_days[d1] and new_days[d2]:
                i1 = random.randint(0, len(new_days[d1]) - 1)
                i2 = random.randint(0, len(new_days[d2]) - 1)
                p1, p2 = new_days[d1][i1], new_days[d2][i2]
                # Check day-fixed and pos-locked constraints
                if p1 in pos_locked or p2 in pos_locked:
                    continue
                if fixed.get(p1) not in (None, d2) or fixed.get(p2) not in (None, d1):
                    continue
                new_days[d1][i1], new_days[d2][i2] = p2, p1
        else:
            # Move one place to a different day
            d1 = random.randint(0, d - 1)
            d2 = random.randint(0, d - 1)
            if d1 == d2 or not new_days[d1]:
                continue
            if len(new_days[d2]) >= per_day_max or len(new_days[d1]) <= per_day_min:
                continue
            idx = random.randint(0, len(new_days[d1]) - 1)
            p = new_days[d1][idx]
            if p in pos_locked:
                continue
            if fixed.get(p) not in (None, d2):
                continue
            new_days[d1].pop(idx)
            new_days[d2].append(p)

        new_cost = total_cost(new_days, dist, pos_constraints)

        delta = new_cost - cost
        if delta < 0 or random.random() < math.exp(-delta / (temp * 5)):
            days = new_days
            cost = new_cost
            if cost < best_cost:
                best_cost = cost
                best_days = copy.deepcopy(days)

    return best_days, best_cost


def score_solution(solution_indices, dist, pos_constraints=None):
    """Score a given day assignment."""
    pc = pos_constraints or {}
    details = []
    total = 0.0
    for di, day in enumerate(solution_indices):
        if day:
            _, km = nn_tsp_cost(day, dist, pc.get(di))
        else:
            km = 0.0
        details.append({"day": di + 1, "count": len(day), "intra_km": round(km, 1)})
        total += km
    return round(total, 1), details


def main():
    input_data = json.load(sys.stdin)
    t0 = time.time()

    places = input_data["places"]
    n = len(places)
    d = input_data["days"]
    per_day_min = input_data.get("per_day_min", 2)
    per_day_max = input_data.get("per_day_max", 6)
    top_n = input_data.get("top_n", 5)
    iterations = input_data.get("iterations", 5000)
    restarts = input_data.get("restarts", 5)

    # --- Validation: fail fast on all constraint errors ---
    errors = []

    # Capacity check
    if n > d * per_day_max:
        errors.append(f"capacity: {n} places cannot fit in {d} days × {per_day_max} max/day = {d * per_day_max} slots")
    if n < d * per_day_min:
        errors.append(f"capacity: {n} places < {d} days × {per_day_min} min/day = {d * per_day_min} required")

    # Build name→index mapping
    name_to_idx = {p["name"]: i for i, p in enumerate(places)}

    # Check for duplicate place names
    if len(name_to_idx) != n:
        seen = {}
        for i, p in enumerate(places):
            if p["name"] in seen:
                errors.append(f"duplicate place name: \"{p['name']}\" at index {seen[p['name']]} and {i}")
            seen[p["name"]] = i

    # Check for missing coordinates
    for i, p in enumerate(places):
        if p.get("lat") is None or p.get("lng") is None:
            errors.append(f"missing coordinates: \"{p['name']}\" (index {i})")

    # Parse fixed constraints (supports both formats):
    #   "fixed": {"景點A": 2}                      → day-only (legacy)
    #   "fixed": {"景點A": {"day": 2, "pos": 1}}   → day + position
    #   pos: 1-indexed int for position, "last" for last slot
    fixed_raw = input_data.get("fixed", {})
    fixed = {}           # {place_idx: day_idx_0based}
    pos_constraints = {} # {day_idx_0based: {place_idx: position_0based}}

    # Track position slots for conflict detection: {(day_0, pos_0): place_name}
    pos_slots = {}

    for name, constraint in fixed_raw.items():
        if name not in name_to_idx:
            errors.append(f"fixed: \"{name}\" not found in places list")
            continue
        idx = name_to_idx[name]

        if isinstance(constraint, int):
            day_1 = constraint
            if day_1 < 1 or day_1 > d:
                errors.append(f"fixed: \"{name}\" day={day_1} out of range 1-{d}")
                continue
            fixed[idx] = day_1 - 1
        elif isinstance(constraint, dict):
            day_num = constraint.get("day")
            pos = constraint.get("pos")

            if day_num is None:
                errors.append(f"fixed: \"{name}\" dict format requires \"day\" key")
                continue
            if day_num < 1 or day_num > d:
                errors.append(f"fixed: \"{name}\" day={day_num} out of range 1-{d}")
                continue

            day_0 = day_num - 1
            fixed[idx] = day_0

            if pos is not None:
                if pos == "last":
                    pos_0 = -1
                elif isinstance(pos, int) and pos >= 1:
                    if pos > per_day_max:
                        errors.append(f"fixed: \"{name}\" pos={pos} exceeds per_day_max={per_day_max}")
                        continue
                    pos_0 = pos - 1
                else:
                    errors.append(f"fixed: \"{name}\" invalid pos={pos!r} (use 1-indexed int or \"last\")")
                    continue

                # Check for slot conflicts
                slot_key = (day_0, pos_0)
                if slot_key in pos_slots:
                    other = pos_slots[slot_key]
                    pos_label = "last" if pos_0 == -1 else pos_0 + 1
                    errors.append(f"fixed: position conflict on day {day_num} pos {pos_label}: \"{name}\" vs \"{other}\"")
                    continue
                pos_slots[slot_key] = name

                if day_0 not in pos_constraints:
                    pos_constraints[day_0] = {}
                pos_constraints[day_0][idx] = pos_0
        else:
            errors.append(f"fixed: \"{name}\" invalid value {constraint!r} (use int or dict)")

    # Check fixed-to-day counts don't exceed per_day_max
    from collections import Counter
    day_fixed_counts = Counter(fixed.values())
    for day_0, count in day_fixed_counts.items():
        if count > per_day_max:
            errors.append(f"fixed: day {day_0 + 1} has {count} fixed places but per_day_max={per_day_max}")

    # --- Abort if any errors ---
    if errors:
        error_output = {"errors": errors, "count": len(errors)}
        json.dump(error_output, sys.stdout, ensure_ascii=False, indent=2)
        print(file=sys.stdout)
        sys.exit(1)

    # Handle start point (shorthand: sets pos=1 on all days)
    start_name = input_data.get("start")
    start_idx = name_to_idx.get(start_name) if start_name else None

    # Build distance matrix
    dist = build_distance_matrix(places)

    # Run SA with multiple restarts
    all_results = []
    for r in range(restarts):
        best_days, best_cost = sa_optimize(
            n, d, dist, per_day_min, per_day_max, fixed, pos_constraints,
            iterations, seed=r * 7 + 1,
        )
        all_results.append((best_cost, best_days))

    # Deduplicate and sort
    all_results.sort(key=lambda x: x[0])
    seen_scores = set()
    unique_results = []
    for cost, days in all_results:
        rounded = round(cost, 1)
        if rounded not in seen_scores:
            seen_scores.add(rounded)
            unique_results.append((cost, days))
        if len(unique_results) >= top_n:
            break

    # Format output
    solutions = []
    for rank, (cost, day_indices) in enumerate(unique_results):
        score, details = score_solution(day_indices, dist, pos_constraints)
        # Convert indices to names, apply NN ordering with pos constraints
        days_named = {}
        for di, day in enumerate(day_indices):
            if day:
                day_pc = pos_constraints.get(di)
                ordered, _ = nn_tsp_cost(day, dist, day_pc)
                # If start point exists and is in this day and no pos constraint overrides, put it first
                if (start_idx is not None and start_idx in ordered
                        and start_idx not in (pos_constraints.get(di) or {})):
                    ordered.remove(start_idx)
                    ordered.insert(0, start_idx)
                days_named[str(di + 1)] = [places[i]["name"] for i in ordered]
            else:
                days_named[str(di + 1)] = []
        solutions.append({
            "rank": rank + 1,
            "score_km": score,
            "days": days_named,
            "day_details": details,
        })

    # Score AI solution if provided
    ai_comparison = None
    ai_solution = input_data.get("ai_solution")
    if ai_solution:
        ai_indices = []
        for day_key in sorted(ai_solution.keys(), key=int):
            day_names = ai_solution[day_key]
            day_idx = [name_to_idx[n] for n in day_names if n in name_to_idx]
            ai_indices.append(day_idx)
        ai_score, _ = score_solution(ai_indices, dist)
        best_score = unique_results[0][0] if unique_results else ai_score
        delta_pct = round((ai_score - best_score) / best_score * 100, 1) if best_score > 0 else 0
        rank_among = sum(1 for c, _ in unique_results if c < ai_score) + 1

        if delta_pct < 5:
            verdict = "optimal"
        elif delta_pct < 30:
            verdict = "acceptable"
        else:
            verdict = "inefficient"

        ai_comparison = {
            "score_km": ai_score,
            "rank_among_top_n": rank_among,
            "delta_vs_best_pct": delta_pct,
            "verdict": verdict,
        }

    elapsed_ms = round((time.time() - t0) * 1000)

    output = {
        "solutions": solutions,
        "compute_time_ms": elapsed_ms,
        "config": {
            "places": n,
            "days": d,
            "iterations": iterations,
            "restarts": restarts,
            "per_day_min": per_day_min,
            "per_day_max": per_day_max,
        },
    }
    if ai_comparison:
        output["ai_comparison"] = ai_comparison

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
