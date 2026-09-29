#!/usr/bin/env python3
"""MLB market odds loader: Oddize primary, The Odds API fallback.

Oddize is queried first for only the prop types required by today's model
predictions. If Oddize fails, returns an unusable schema, or returns no usable
rows for a required market while games remain upcoming, the existing
load_mlb_market_odds.py loader is invoked as the fallback provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from supabase import create_client
import load_mlb_market_odds as legacy

ODDIZE_BASE = "https://oddize.com/api/v1"
ET = ZoneInfo("America/New_York")
ODDIZE_MARKETS = {
    "batter_hits": "batter_h",
    "batter_total_bases": "batter_tb",
    "batter_home_runs": "batter_hr",
    "pitcher_strikeouts": "pitcher_k",
}
BATTER_LINES = {
    "batter_hits": 0.5,
    "batter_total_bases": 1.5,
    "batter_home_runs": 0.5,
}
MARKET_NAMES = {
    "batter_hits": "Batter Hits",
    "batter_total_bases": "Batter Total Bases",
    "batter_home_runs": "Batter Home Runs",
    "pitcher_strikeouts": "Pitcher Strikeouts",
}


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").lower().replace(".", "").replace("'", "").replace("-", " ").split())


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def eastern_date(value: Any) -> str | None:
    dt = parse_dt(value)
    return dt.astimezone(ET).date().isoformat() if dt else None


def american_to_decimal(price: int) -> float:
    return 1 + (100 / abs(price) if price < 0 else price / 100)


def american_to_implied(price: int) -> float:
    return abs(price) / (abs(price) + 100) if price < 0 else 100 / (price + 100)


def stable_key(row: dict[str, Any], pitcher: bool = False) -> str:
    parts = [row.get("odds_provider", ""), row.get("book_name", ""), row.get("provider_event_id", "")]
    parts.append(row.get("pitcher_name_raw", "") if pitcher else row.get("player_name_raw", ""))
    parts += [row.get("market_key", ""), str(row.get("line", "")), row.get("outcome_name", "")]
    return hashlib.md5("|".join(map(str, parts)).lower().encode()).hexdigest()


class OddizeUsage:
    def __init__(self) -> None:
        self.requests = 0
        self.credits = 0
        self.remaining: str | None = None

    def record(self, headers: dict[str, str]) -> None:
        self.requests += 1
        try:
            self.credits += int(headers.get("x-credits-cost", "0"))
        except ValueError:
            pass
        self.remaining = headers.get("x-credits-remaining", self.remaining)


def oddize_get(path: str, params: dict[str, Any], key: str, usage: OddizeUsage, attempts: int = 3) -> Any:
    url = f"{ODDIZE_BASE}{path}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    req = Request(url, headers={"X-API-Key": key, "User-Agent": "mlb-hit-lab/3.0", "Accept": "application/json"})
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(req, timeout=45) as response:
                headers = {k.lower(): v for k, v in response.headers.items()}
                usage.record(headers)
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt == attempts:
                raise RuntimeError(f"Oddize HTTP {exc.code}: {body[:500]}") from exc
        except (URLError, ConnectionResetError, TimeoutError, OSError) as exc:
            if attempt == attempts:
                raise RuntimeError(f"Oddize network failure after {attempts} attempts: {exc}") from exc
        time.sleep(2 ** (attempt - 1))
    raise RuntimeError("Oddize request failed")


def iter_records(node: Any, inherited: dict[str, Any] | None = None):
    """Flatten documented/nested prop payloads while retaining event context."""
    inherited = dict(inherited or {})
    if isinstance(node, dict):
        context_keys = ("event_id", "game_id", "start_date", "commence_time", "home_team", "away_team", "team1", "team2", "sport", "prop_type")
        for key in context_keys:
            if node.get(key) is not None:
                inherited[key] = node[key]
        price_keys = {"american_odds", "price", "odds"}
        book_keys = {"book", "book_name", "sportsbook", "bookmaker"}
        if price_keys.intersection(node) and book_keys.intersection(node):
            merged = dict(inherited)
            merged.update(node)
            yield merged
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from iter_records(value, inherited)
    elif isinstance(node, list):
        for item in node:
            yield from iter_records(item, inherited)


def first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def side_from(row: dict[str, Any]) -> str | None:
    raw = str(first(row, "side", "outcome", "outcome_name", "team", "direction") or "").strip().lower()
    if raw in {"over", "o", "yes"}:
        return "over"
    if raw in {"under", "u", "no"}:
        return "under"
    return None


def match_game(row: dict[str, Any], game_meta: dict[int, dict[str, Any]], target_date: str) -> int | None:
    event_id = str(first(row, "event_id", "game_id") or "")
    # Oddize MLB IDs commonly encode YYYYMMDD; use team matching as authority.
    row_date = eastern_date(first(row, "start_date", "commence_time", "start_time"))
    if row_date and row_date != target_date:
        return None
    home = normalize_name(first(row, "home_team", "team2"))
    away = normalize_name(first(row, "away_team", "team1"))
    matches = []
    for game_pk, meta in game_meta.items():
        mh = normalize_name(meta.get("home_team_name"))
        ma = normalize_name(meta.get("away_team_name"))
        if home and away and home == mh and away == ma:
            matches.append(game_pk)
        elif event_id and normalize_name(meta.get("home_team_name")) in normalize_name(event_id):
            # Never accept this alone; retained only for diagnostics/future schemas.
            pass
    return matches[0] if len(matches) == 1 else None


def parse_price(value: Any) -> int | None:
    try:
        return int(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None


def parse_line(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_market_rows(payload: Any, market_key: str, target_date: str, game_meta: dict[int, dict[str, Any]],
                        pitcher_by_name: dict[str, list[dict[str, Any]]], master_by_name: dict[str, list[dict[str, Any]]]):
    batter_rows: list[dict[str, Any]] = []
    pitcher_rows: list[dict[str, Any]] = []
    for src in iter_records(payload):
        side = side_from(src)
        price = parse_price(first(src, "american_odds", "price", "odds"))
        line = parse_line(first(src, "line", "point", "threshold"))
        player = first(src, "player_name", "player", "subject", "participant", "description", "name")
        book = str(first(src, "book", "book_name", "sportsbook", "bookmaker") or "unknown").lower()
        if not side or price is None or line is None or not player:
            continue
        game_pk = match_game(src, game_meta, target_date)
        if game_pk is None:
            continue
        meta = game_meta[game_pk]
        event_id = str(first(src, "event_id", "game_id") or game_pk)
        commence = first(src, "start_date", "commence_time", "start_time") or meta.get("game_time_utc")
        updated = first(src, "timestamp", "updated_at", "last_update", "ts")
        if isinstance(updated, (int, float)):
            updated = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat()
        fetched = datetime.now(timezone.utc).isoformat()

        if market_key in BATTER_LINES:
            if line != BATTER_LINES[market_key]:
                continue
            row = {
                "odds_provider": "oddize", "book_name": book, "provider_event_id": event_id,
                "provider_market_id": ODDIZE_MARKETS[market_key], "game_pk": game_pk, "game_date": target_date,
                "commence_time_utc": commence, "home_team": meta.get("home_team_name"), "away_team": meta.get("away_team_name"),
                "player_id": None, "player_name_raw": str(player), "market_key": market_key,
                "market_name": MARKET_NAMES[market_key], "line": line, "outcome_name": side,
                "american_odds": price, "decimal_odds": None, "odds_last_update": updated,
                "fetched_at": fetched, "raw_payload": {"oddize": src},
            }
            row["load_key"] = stable_key(row)
            batter_rows.append(row)
        else:
            norm = normalize_name(player)
            matches = pitcher_by_name.get(norm, [])
            resolved = matches[0] if len(matches) == 1 else {}
            masters = master_by_name.get(norm, [])
            master = masters[0] if len(masters) == 1 else {}
            row = {
                "odds_provider": "oddize", "book_name": book, "provider_event_id": event_id,
                "provider_market_id": ODDIZE_MARKETS[market_key], "game_pk": resolved.get("game_pk") or game_pk,
                "game_date": target_date, "commence_time_utc": commence,
                "home_team": meta.get("home_team_name"), "away_team": meta.get("away_team_name"),
                "pitcher_id": resolved.get("pitcher_id") or master.get("pitcher_id"), "pitcher_name_raw": str(player),
                "normalized_pitcher_name": norm, "market_key": market_key, "market_name": MARKET_NAMES[market_key],
                "line": line, "outcome_name": side, "american_odds": price,
                "decimal_odds": american_to_decimal(price), "implied_probability": american_to_implied(price),
                "odds_last_update": updated, "fetched_at": fetched, "raw_payload": {"oddize": src},
            }
            row["load_key"] = stable_key(row, pitcher=True)
            pitcher_rows.append(row)
    return batter_rows, pitcher_rows


def run_legacy(reason: str) -> int:
    print(f"FALLBACK ACTIVATED: {reason}", file=sys.stderr)
    print("Falling back to existing The Odds API consolidated loader.", file=sys.stderr)
    return legacy.main()


def main() -> int:
    sb = create_client(require("SUPABASE_URL"), require("SUPABASE_SERVICE_ROLE_KEY"))
    oddize_key = os.getenv("ODDIZE_API_KEY")
    if not oddize_key:
        return run_legacy("ODDIZE_API_KEY is not configured")

    target_date = os.getenv("ODDS_TARGET_DATE") or datetime.now(ET).date().isoformat()
    required, game_meta, pitcher_by_name, master_by_name = legacy.load_prediction_requirements(sb, target_date)
    print(f"Odds target date: {target_date}")
    print(f"Primary provider: Oddize | fallback: The Odds API")
    print(f"Games with model predictions: {len(required)}")
    if not required:
        print("No model predictions found. No provider credits will be spent.")
        return 0

    needed = sorted({m for markets in required.values() for m in markets})
    usage = OddizeUsage()
    all_batter: list[dict[str, Any]] = []
    all_pitcher: list[dict[str, Any]] = []
    usable_by_market: dict[str, int] = {}

    try:
        for market_key in needed:
            oddize_type = ODDIZE_MARKETS[market_key]
            print(f"Oddize: fetching MLB prop_type={oddize_type} ({market_key})")
            payload = oddize_get(f"/props/mlb", {"prop_type": oddize_type}, oddize_key, usage)
            b_rows, p_rows = extract_market_rows(payload, market_key, target_date, game_meta, pitcher_by_name, master_by_name)
            # Keep only games where our models require this market.
            b_rows = [r for r in b_rows if market_key in required.get(int(r["game_pk"]), set())]
            p_rows = [r for r in p_rows if market_key in required.get(int(r["game_pk"]), set())]
            all_batter.extend(b_rows)
            all_pitcher.extend(p_rows)
            usable_by_market[market_key] = len(b_rows) + len(p_rows)
            print(f"  usable rows={usable_by_market[market_key]}")
    except Exception as exc:  # noqa: BLE001
        return run_legacy(f"Oddize request/parse failure: {exc}")

    missing = [m for m in needed if usable_by_market.get(m, 0) == 0]
    if missing:
        return run_legacy(f"Oddize returned zero usable rows for required market(s): {','.join(missing)}")

    # Only replace snapshots after all required Oddize markets pass validation.
    if any(m in BATTER_LINES for m in needed):
        result = sb.rpc("replace_mlb_batter_prop_market_odds_snapshot", {
            "p_odds_provider": "oddize", "p_game_date": target_date, "p_rows": all_batter,
        }).execute()
        print("Oddize batter snapshot result:")
        print(json.dumps(result.data, indent=2, default=str))

    if "pitcher_strikeouts" in needed and all_pitcher:
        for i in range(0, len(all_pitcher), 500):
            sb.table("mlb_pitcher_k_market_odds").upsert(all_pitcher[i:i+500], on_conflict="load_key").execute()

    print(f"Oddize batter rows: {len(all_batter)}")
    print(f"Oddize pitcher-K rows: {len(all_pitcher)}")
    print("Oddize API usage:")
    print(json.dumps({"http_requests_made": usage.requests, "credits_this_run": usage.credits,
                      "credits_remaining": usage.remaining}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
