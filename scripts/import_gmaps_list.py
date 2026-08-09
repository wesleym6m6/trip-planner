"""
Import places from a shared Google Maps list URL.

Given a shared Google Maps list URL, extracts all places (name, coordinates,
notes) and outputs them as JSON. Optionally merges into an existing
itinerary.json for a trip directory.

Usage:
    # Print JSON to stdout
    python3 scripts/import_gmaps_list.py "https://maps.app.goo.gl/XXXXX"

    # Merge into existing trip itinerary (append to day N or create new day)
    python3 scripts/import_gmaps_list.py --merge trips/{slug} --day 3 "URL"

Reference: TREK placeService.ts importGoogleList (lines 320-405)
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests

if __package__:
    from .plan_compat import CanonicalWriteRefused, refuse_canonical_write
else:
    from plan_compat import CanonicalWriteRefused, refuse_canonical_write

# Google Maps internal API endpoint for fetching list data
GETLIST_URL = (
    "https://www.google.com/maps/preview/entitylist/getlist"
    "?authuser=0&hl=en&gl=us"
    "&pb=!1m1!1s{list_id}!2e2!3e2!4i500!16b1"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 15


def resolve_url(url: str) -> str:
    """Follow redirects for short URLs (goo.gl, maps.app) to get full URL."""
    if "goo.gl" in url or "maps.app" in url:
        try:
            resp = requests.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
            return resp.url
        except requests.RequestException as e:
            print(f"[ERROR] Failed to resolve short URL: {e}", file=sys.stderr)
            sys.exit(1)
    return url


def extract_list_id(url: str) -> str:
    """Extract Google Maps list ID from URL.

    Patterns:
      1. /placelists/list/{ID}
      2. !2s{ID} in URL params (IDs are 15+ characters)
    """
    # Pattern 1: /placelists/list/{ID}
    match = re.search(r"placelists/list/([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)

    # Pattern 2: !2s{ID} in data URL params
    match = re.search(r"!2s([A-Za-z0-9_-]{15,})", url)
    if match:
        return match.group(1)

    print(
        "[ERROR] Could not extract list ID from URL. "
        "Please use a shared Google Maps list link.\n"
        f"  Resolved URL: {url}",
        file=sys.stderr,
    )
    sys.exit(1)


def fetch_list_data(list_id: str) -> dict:
    """Fetch raw list data from Google Maps internal API."""
    api_url = GETLIST_URL.format(list_id=quote(list_id, safe=""))
    try:
        resp = requests.get(
            api_url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[ERROR] Request to Google Maps failed: {e}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 404:
        print(
            "[ERROR] List not found (404). It may be deleted or the ID is wrong.",
            file=sys.stderr,
        )
        sys.exit(1)

    if resp.status_code != 200:
        print(
            f"[ERROR] Google Maps returned HTTP {resp.status_code}.",
            file=sys.stderr,
        )
        sys.exit(1)

    raw_text = resp.text
    if not raw_text.strip():
        print(
            "[ERROR] Empty response from Google Maps. "
            "The list may be private or inaccessible.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Skip first line (metadata prefix like ")]}'")
    newline_idx = raw_text.index("\n") if "\n" in raw_text else -1
    if newline_idx == -1:
        json_str = raw_text
    else:
        json_str = raw_text[newline_idx + 1 :]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(
            f"[ERROR] Failed to parse Google Maps response as JSON: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    return data


def parse_places(list_data: list) -> tuple[str, list[dict]]:
    """Parse place data from raw Google Maps list response.

    Returns (list_name, places) where places is a list of
    {title, lat, lng, note} dicts.
    """
    meta = list_data[0] if list_data else None
    if not meta:
        print(
            "[ERROR] Invalid list data received from Google Maps.",
            file=sys.stderr,
        )
        sys.exit(1)

    list_name = meta[4] if len(meta) > 4 and meta[4] else "Google Maps List"
    items = meta[8] if len(meta) > 8 else None

    if not isinstance(items, list) or len(items) == 0:
        print(
            "[ERROR] List is empty or could not be read. "
            "It may be private or contain no saved places.",
            file=sys.stderr,
        )
        sys.exit(1)

    places = []
    skipped = 0
    for item in items:
        name = item[2] if len(item) > 2 else None
        note = item[3] if len(item) > 3 and item[3] else None

        # Coordinates: item[1][5][2] = lat, item[1][5][3] = lng
        coords = None
        try:
            coords = item[1][5]
        except (TypeError, IndexError):
            pass

        lat = None
        lng = None
        if coords and len(coords) > 3:
            lat = coords[2]
            lng = coords[3]

        if (
            name
            and isinstance(lat, (int, float))
            and isinstance(lng, (int, float))
        ):
            places.append({
                "title": name,
                "lat": lat,
                "lng": lng,
                "note": note,
            })
        else:
            skipped += 1
            label = name or "(unnamed)"
            print(
                f"[WARN] Skipping place without coordinates: {label}",
                file=sys.stderr,
            )

    if len(places) == 0:
        print(
            "[ERROR] No places with valid coordinates found in list.",
            file=sys.stderr,
        )
        sys.exit(1)

    if skipped > 0:
        print(
            f"[INFO] Skipped {skipped} item(s) without coordinates.",
            file=sys.stderr,
        )

    return list_name, places


def merge_into_itinerary(
    trip_dir: str, places: list[dict], list_name: str, target_day: int | None
) -> None:
    """Merge imported places into an existing itinerary.json.

    If target_day is given, append places to that day's places array.
    Otherwise, create a new day titled with the list name.
    """
    data_dir = Path(trip_dir) / "data"
    refuse_canonical_write(data_dir, operation="import_gmaps_list.py --merge")
    itinerary_path = data_dir / "itinerary.json"
    try:
        with open(itinerary_path, "r", encoding="utf-8") as f:
            itinerary = json.load(f)
    except FileNotFoundError:
        print(
            f"[ERROR] Itinerary not found at {itinerary_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(
            f"[ERROR] Failed to parse {itinerary_path}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Convert imported places to itinerary format
    itinerary_places = []
    for p in places:
        entry = {
            "type": "spot",
            "title": p["title"],
            "lat": p["lat"],
            "lng": p["lng"],
        }
        if p["note"]:
            entry["note"] = p["note"]
        itinerary_places.append(entry)

    days = itinerary.get("days", [])

    if target_day is not None:
        # Find the target day and append
        found = False
        for day in days:
            if day.get("day") == target_day:
                day.setdefault("places", []).extend(itinerary_places)
                found = True
                print(
                    f"[INFO] Appended {len(itinerary_places)} place(s) to day {target_day}.",
                    file=sys.stderr,
                )
                break
        if not found:
            print(
                f"[ERROR] Day {target_day} not found in itinerary. "
                f"Available days: {[d.get('day') for d in days]}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Create a new day
        max_day = max((d.get("day", 0) for d in days), default=0)
        new_day = {
            "day": max_day + 1,
            "title": f"Imported: {list_name}",
            "subtitle": f"從 Google Maps 清單匯入（{len(itinerary_places)} 個地點）",
            "places": itinerary_places,
            "travel": [],
        }
        days.append(new_day)
        print(
            f"[INFO] Created new day {max_day + 1} with {len(itinerary_places)} place(s).",
            file=sys.stderr,
        )

    itinerary["days"] = days

    with open(itinerary_path, "w", encoding="utf-8") as f:
        json.dump(itinerary, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"[INFO] Saved to {itinerary_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Import places from a shared Google Maps list URL."
    )
    parser.add_argument(
        "url",
        help="Shared Google Maps list URL",
    )
    parser.add_argument(
        "--merge",
        metavar="TRIP_DIR",
        help="Trip directory to merge into (e.g. trips/{slug})",
    )
    parser.add_argument(
        "--day",
        type=int,
        default=None,
        help="Day number to append places to (used with --merge). "
        "If omitted, creates a new day.",
    )
    args = parser.parse_args()

    if args.merge:
        try:
            refuse_canonical_write(
                Path(args.merge) / "data",
                operation="import_gmaps_list.py --merge",
            )
        except CanonicalWriteRefused as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(2)

    # Validate URL format
    if not args.url.startswith("http"):
        print(
            "[ERROR] Invalid URL. Must start with http:// or https://",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 1: Resolve short URLs
    print(f"[INFO] Resolving URL...", file=sys.stderr)
    resolved_url = resolve_url(args.url)
    if resolved_url != args.url:
        print(f"[INFO] Resolved to: {resolved_url}", file=sys.stderr)

    # Step 2: Extract list ID
    list_id = extract_list_id(resolved_url)
    print(f"[INFO] List ID: {list_id}", file=sys.stderr)

    # Step 3: Fetch list data
    print(f"[INFO] Fetching list data from Google Maps...", file=sys.stderr)
    raw_data = fetch_list_data(list_id)

    # Step 4: Parse places
    list_name, places = parse_places(raw_data)
    print(
        f"[INFO] Found {len(places)} place(s) in list: {list_name}",
        file=sys.stderr,
    )

    # Step 5: Output or merge
    if args.merge:
        merge_into_itinerary(args.merge, places, list_name, args.day)
    else:
        json.dump(places, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
