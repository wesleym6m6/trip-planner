"""
Search flights via SerpApi Google Flights engine.

Round-trip (type=1) requires three separate searches:
  1. Initial search → returns OUTBOUND legs only (each with departure_token)
  2. + departure_token → returns RETURN legs for that outbound (each with booking_token)
  3. + booking_token  → returns OTA booking links + prices

Input (stdin JSON):
{
  "departure_id": "AAA",
  "arrival_id": "BBB",
  "outbound_date": "2099-01-01",
  "return_date": "2099-01-05",
  "cache_path": "trips/{slug}/data/flights_cache.json"
}

Stage 2 adds: "departure_token": "<from stage 1 result>"
Stage 3 adds: "booking_token": "<from stage 2 result>"

Required: departure_id, arrival_id, outbound_date, cache_path
          (return_date required when type=1 round-trip)

Optional overrides: type, adults, currency, hl, gl, stops, travel_class,
  sort_by, include_airlines, exclude_airlines, max_price, max_duration,
  outbound_times, return_times, bags, emissions, layover_duration,
  exclude_conns, children, infants_in_seat, infants_on_lap,
  departure_token, booking_token, lcc_only

Full parameter docs: docs/serpapi-flights-params.md
"""
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from serpapi_common import add_fetched_at, serpapi_search, write_cache

DEFAULTS = {
    "type": 1,
    "adults": 1,
    "currency": "TWD",
    "hl": "zh-TW",
    "gl": "tw",
}

# Asia-Pacific LCC IATA codes
LCC_AIRLINES = {
    "VJ",  # VietJet Air
    "QZ",  # Indonesia AirAsia
    "AK",  # AirAsia (Malaysia)
    "FD",  # Thai AirAsia
    "Z2",  # AirAsia Philippines
    "D7",  # AirAsia X
    "XJ",  # Thai AirAsia X
    "XT",  # Indonesia AirAsia X
    "IT",  # Tigerair Taiwan
    "TR",  # Scoot
    "TZ",  # Scoot (old IATA)
    "MM",  # Peach Aviation
    "GK",  # Jetstar Japan
    "JW",  # Vanilla Air (merged into Peach)
    "3K",  # Jetstar Asia
    "JQ",  # Jetstar Airways
    "BL",  # Pacific Airlines (ex Jetstar Pacific)
    "5J",  # Cebu Pacific
    "DG",  # Cebgo
    "SL",  # Thai Lion Air
    "JT",  # Lion Air
    "IW",  # Wings Air
    "OD",  # Batik Air Malaysia
    "TW",  # T'way Air
    "LJ",  # Jin Air
    "7C",  # Jeju Air
    "BX",  # Air Busan
    "ZE",  # Eastar Jet
    "RS",  # Air Seoul
    "9C",  # Spring Airlines
    "HO",  # Juneyao Airlines
    "GX",  # Guangxi Beibu Gulf Airlines
    "DR",  # Ruili Airlines
    "PN",  # China West Air
    "TV",  # Tibet Airlines
}

REQUIRED_FIELDS = {"departure_id", "arrival_id", "outbound_date", "cache_path"}

# All optional SerpApi params that can be passed through
OPTIONAL_PARAMS = {
    "type", "adults", "currency", "hl", "gl", "stops", "travel_class",
    "sort_by", "include_airlines", "exclude_airlines", "max_price",
    "max_duration", "outbound_times", "return_times", "bags", "emissions",
    "layover_duration", "exclude_conns", "children", "infants_in_seat",
    "infants_on_lap", "departure_token", "booking_token", "return_date",
}


def build_params(user_input):
    """Merge DEFAULTS + user_input into SerpApi params."""
    merged = {**DEFAULTS, **user_input}

    params = {"engine": "google_flights", "show_hidden": True}

    # Required fields
    for key in ("departure_id", "arrival_id", "outbound_date"):
        params[key] = merged[key]

    # return_date required for round-trip (type=1)
    if merged.get("type", 1) == 1:
        if "return_date" not in merged:
            raise ValueError("return_date is required for round-trip (type=1)")
        params["return_date"] = merged["return_date"]
    elif "return_date" in merged:
        params["return_date"] = merged["return_date"]

    # Token-based queries (second-stage)
    for token_key in ("departure_token", "booking_token"):
        if token_key in merged:
            params[token_key] = merged[token_key]

    # All other optional params
    for key in OPTIONAL_PARAMS - {"departure_token", "booking_token", "return_date"}:
        if key in merged and merged[key] is not None:
            params[key] = merged[key]

    return params


def extract_flights(raw):
    """Extract flight data, removing search_metadata/search_parameters."""
    result = {}
    for key, value in raw.items():
        if key in ("search_metadata", "search_parameters"):
            continue
        result[key] = value
    return result


def _extract_iata(leg):
    """Extract IATA code from a flight leg.

    Tries flight_number first (e.g. 'IT 551' → 'IT'),
    then falls back to airline_logo URL (e.g. '.../70px/IT.png' → 'IT').
    """
    fn = leg.get("flight_number", "")
    if fn:
        parts = fn.split()
        if parts and len(parts[0]) == 2:
            return parts[0]
    logo = leg.get("airline_logo", "")
    if logo:
        # URL like https://www.gstatic.com/flights/airline_logos/70px/IT.png
        basename = logo.rsplit("/", 1)[-1]
        code = basename.split(".")[0]
        if len(code) == 2:
            return code
    return None


def tag_lcc(flights):
    """Add is_lcc flag to each flight based on airline IATA codes."""
    for flight in flights:
        codes = set()
        for leg in flight.get("flights", []):
            code = _extract_iata(leg)
            if code:
                codes.add(code)
        flight["is_lcc"] = bool(codes & LCC_AIRLINES)
    return flights


def filter_lcc(flights):
    """Keep only flights where all legs are LCC."""
    result = []
    for flight in flights:
        codes = set()
        for leg in flight.get("flights", []):
            code = _extract_iata(leg)
            if code:
                codes.add(code)
        if codes and codes.issubset(LCC_AIRLINES):
            result.append(flight)
    return result


def summarize_flight(flight, index):
    """Extract key fields for stdout summary (keeps cache intact)."""
    legs = flight.get("flights", [])
    first_leg = legs[0] if legs else {}
    last_leg = legs[-1] if legs else first_leg
    return {
        "index": index,
        "airline": first_leg.get("airline", "?"),
        "flight_number": first_leg.get("flight_number", "?"),
        "departure": first_leg.get("departure_airport", {}).get("time", "?"),
        "arrival": last_leg.get("arrival_airport", {}).get("time", "?"),
        "duration": flight.get("total_duration"),
        "price": flight.get("price"),
        "is_lcc": flight.get("is_lcc", False),
        "aircraft": first_leg.get("airplane", "?"),
        "legroom": first_leg.get("legroom"),
        "often_delayed": first_leg.get("often_delayed_by_over_30_min", False),
        "stops": len(legs) - 1,
        "departure_token": flight.get("departure_token"),
    }


def main():
    user_input = json.load(sys.stdin)

    missing = REQUIRED_FIELDS - set(user_input.keys())
    if missing:
        print(f"Missing required fields: {', '.join(sorted(missing))}", file=sys.stderr)
        sys.exit(1)

    cache_path = user_input.pop("cache_path")
    lcc_only = user_input.pop("lcc_only", False)

    params = build_params(user_input)
    print(f"Searching flights: {params.get('departure_id')} → {params.get('arrival_id')} "
          f"({params.get('outbound_date')})", file=sys.stderr)

    raw = serpapi_search(params)
    data = extract_flights(raw)

    # Tag and optionally filter LCC
    for key in ("best_flights", "other_flights"):
        if key in data:
            data[key] = tag_lcc(data[key])
            add_fetched_at(data[key])

    if lcc_only:
        for key in ("best_flights", "other_flights"):
            if key in data:
                data[key] = filter_lcc(data[key])
        print("LCC filter applied", file=sys.stderr)

    write_cache(cache_path, data)

    # Summary with per-flight key fields
    all_flights = data.get("best_flights", []) + data.get("other_flights", [])
    prices = [f["price"] for f in all_flights if "price" in f]
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = {
        "flights_count": len(all_flights),
        "price_range": [min(prices), max(prices)] if prices else [],
        "cache_path": cache_path,
        "fetched_at": now,
        "flights": [summarize_flight(f, i) for i, f in enumerate(all_flights)],
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
