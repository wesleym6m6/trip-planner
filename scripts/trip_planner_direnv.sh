# shellcheck shell=bash
# Load only allowlisted keys from the private daily runtime record. This file is
# sourced by the local .envrc; it never unlocks Bitwarden or reads BW_SESSION.

trip_dev_bridge_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
trip_dev_session_helper="$trip_dev_bridge_dir/trip_planner_dev_session.py"

if [ -f "$trip_dev_session_helper" ]; then
  if type watch_file >/dev/null 2>&1; then
    watch_file "$trip_dev_session_helper"
  fi
  trip_dev_session_path="$(
    python3 "$trip_dev_session_helper" path 2>/dev/null || true
  )"
  if [ -n "$trip_dev_session_path" ] && type watch_file >/dev/null 2>&1; then
    watch_file "$trip_dev_session_path"
  fi

  if [ -z "${GOOGLE_MAPS_API_KEY:-}" ]; then
    if trip_cached_google_key="$(
      TRIP_PLANNER_DIRENV_BRIDGE=1 \
        python3 "$trip_dev_session_helper" _emit GOOGLE_MAPS_API_KEY 2>/dev/null
    )"; then
      export GOOGLE_MAPS_API_KEY="$trip_cached_google_key"
    fi
    unset trip_cached_google_key
  fi

  if [ -z "${SERPAPI_API_KEY:-}" ]; then
    if trip_cached_serp_key="$(
      TRIP_PLANNER_DIRENV_BRIDGE=1 \
        python3 "$trip_dev_session_helper" _emit SERPAPI_API_KEY 2>/dev/null
    )"; then
      export SERPAPI_API_KEY="$trip_cached_serp_key"
    fi
    unset trip_cached_serp_key
  fi

  unset trip_dev_session_path
fi

unset trip_dev_session_helper
unset trip_dev_bridge_dir
