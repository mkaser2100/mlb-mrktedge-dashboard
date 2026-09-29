#!/usr/bin/env python3
"""Cost-aware MLB market odds loader.

Goals:
- Fetch The Odds API event list once.
- Query only games that have model predictions in Supabase.
- Query only markets needed for each predicted game.
- Combine required batter and pitcher markets into one event-odds request.
- Preserve existing Supabase destinations and downstream views.
- Log provider quota cost from x-requests-* headers.

The Odds API quota is charged by unique markets returned x region-equivalents.
Combining markets reduces HTTP calls, while prediction-aware filtering is what
reduces quota credits.
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

API_BASE = "https://api.the-odds-api.com/v4"
ET = ZoneInfo("America/New_York")
SPORT_KEY = "baseball_mlb"

BATTER_MARKETS = {
    "batter_hits": ("Batter Hits", 0.5),
    "batter_total_bases": ("Batter Total Bases", 1.5),
    "batter_home_runs": ("Batter Home Runs", 0.5),
}
PITCHER_MARKET = "pitcher_strikeouts"

# Prediction source -> provider market.
PREDICTION_MARKETS = {
    ("v3", "hit_1plus"): "batter_hits",
    ("power", "total_bases_2plus"): "batter_total_bases",
    ("power", "home_run_1plus"): "batter_home_runs",
    ("pitcher_k", PITCHER_MARKET): PITCHER_MARKET,
}


def require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def normalize_name(value: str) -> str:
    return " ".join(
        str(value).lower().replace(".", "").replace("'", "").replace("-", " ").split()
    )


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def eastern_date(value: str | None) -> str | None:
    dt = parse_dt(value)
    return dt.astimezone(ET).date().isoformat() if dt else None


def american_to_decimal(price: int) -> float:
    return 1 + (100 / abs(price) if price < 0 else price / 100)


def american_to_implied(price: int) -> float:
    return abs(price) / (abs(price) + 100) if price < 0 else 100 / (price + 100)


def stable_batter_key(row: dict[str, Any]) -> str:
    parts = [
        row.get("odds_provider") or "",
        row.get("book_name") or "",
        row.get("provider_event_id") or "",
        row.get("game_date") or "",
        row.get("player_name_raw") or "",
        row.get("market_key") or "",
        str(row.get("line") or ""),
        (row.get("outcome_name") or "").lower(),
    ]
    return hashlib.md5("|".join(parts).lower().encode()).hexdigest()


def stable_pitcher_key(row: dict[str, Any]) -> str:
    parts = [
        row.get("odds_provider", ""),
        row.get("book_name", ""),
        row.get("provider_event_id", ""),
        row.get("pitcher_name_raw", ""),
        str(row.get("line", "")),
        row.get("outcome_name", ""),
        row.get("odds_last_update", ""),
    ]
    return hashlib.md5("|".join(parts).lower().encode()).hexdigest()


class Usage:
    def __init__(self) -> None:
        self.http_requests = 0
        self.start_used: int | None = None
        self.latest_used: int | None = None
        self.latest_remaining: int | None = None
        self.last_cost: int | None = None
        self.observed_cost = 0

    @staticmethod
    def _int(headers: dict[str, str], key: str) -> int | None:
        try:
            return int(headers[key])
        except (KeyError, TypeError, ValueError):
            return None

    def record(self, headers: dict[str, str]) -> None:
        self.http_requests += 1
        used = self._int(headers, "x-requests-used")
        remaining = self._int(headers, "x-requests-remaining")
        last = self._int(headers, "x-requests-last")
        if self.start_used is None and used is not None:
            # x-requests-used includes this call, so back out x-requests-last.
            self.start_used = used - (last or 0)
        if used is not None:
            self.latest_used = used
        if remaining is not None:
            self.latest_remaining = remaining
        if last is not None:
            self.last_cost = last
            self.observed_cost += last

    def summary(self) -> dict[str, Any]:
        delta = None
        if self.start_used is not None and self.latest_used is not None:
            delta = self.latest_used - self.start_used
        return {
            "http_requests_made": self.http_requests,
            "quota_credits_this_run": delta if delta is not None else self.observed_cost,
            "quota_credits_used_total": self.latest_used,
            "quota_credits_remaining": self.latest_remaining,
            "last_request_cost": self.last_cost,
        }


def get_json(path: str, params: dict[str, Any], usage: Usage, max_attempts: int = 4):
    url = f"{API_BASE}{path}?{urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    req = Request(url, headers={"User-Agent": "mlb-hit-lab/2.0"})
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=45) as response:
                payload = json.loads(response.read().decode())
                headers = {k.lower(): v for k, v in response.headers.items()}
                usage.record(headers)
                return payload
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in {408, 425, 429, 500, 502, 503, 504} or attempt == max_attempts:
                raise RuntimeError(f"Odds API HTTP {exc.code}: {body}") from exc
        except (URLError, ConnectionResetError, TimeoutError, OSError) as exc:
            if attempt == max_attempts:
                raise RuntimeError(f"Odds API request failed after {max_attempts} attempts: {exc}") from exc
        wait = 2 ** (attempt - 1)
        print(f"Transient Odds API error; retrying after {wait}s...")
        time.sleep(wait)
    raise RuntimeError("Odds API request failed unexpectedly")


def load_prediction_requirements(sb, target_date: str):
    """Return game_pk -> required provider markets plus identity helpers."""
    required: dict[int, set[str]] = defaultdict(set)

    v3 = (
        sb.table("mlb_ml_predictions_v3")
        .select("game_pk,target_name")
        .eq("prediction_run_date", target_date)
        .eq("is_active", True)
        .execute().data or []
    )
    for row in v3:
        if row.get("game_pk") is not None and row.get("target_name") == "hit_1plus":
            required[int(row["game_pk"])].add("batter_hits")

    power = (
        sb.table("mlb_ml_batter_predictions")
        .select("game_pk,target_name")
        .eq("prediction_run_date", target_date)
        .in_("target_name", ["home_run_1plus", "total_bases_2plus"])
        .execute().data or []
    )
    for row in power:
        market = {
            "home_run_1plus": "batter_home_runs",
            "total_bases_2plus": "batter_total_bases",
        }.get(row.get("target_name"))
        if market and row.get("game_pk") is not None:
            required[int(row["game_pk"])].add(market)

    pitcher_predictions = (
        sb.table("mlb_ml_pitcher_k_predictions")
        .select("game_pk,pitcher_id,pitcher_name")
        .eq("prediction_run_date", target_date)
        .execute().data or []
    )
    for row in pitcher_predictions:
        if row.get("game_pk") is not None:
            required[int(row["game_pk"])].add(PITCHER_MARKET)

    eligible = (
        sb.table("v_mlb_prediction_eligible_games")
        .select("game_pk,home_team_name,away_team_name,game_time_utc")
        .eq("game_date", target_date)
        .execute().data or []
    )
    game_meta = {
        int(r["game_pk"]): r for r in eligible if r.get("game_pk") is not None
    }

    pitcher_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pitcher_predictions:
        key = normalize_name(row.get("pitcher_name") or "")
        if key:
            pitcher_by_name[key].append(row)

    pitcher_master = (
        sb.table("mlb_pitchers").select("pitcher_id,full_name").execute().data or []
    )
    master_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pitcher_master:
        key = normalize_name(row.get("full_name") or "")
        if key:
            master_by_name[key].append(row)

    return required, game_meta, pitcher_by_name, master_by_name


def match_provider_event(event: dict[str, Any], game_meta: dict[int, dict[str, Any]]) -> int | None:
    home = normalize_name(event.get("home_team") or "")
    away = normalize_name(event.get("away_team") or "")
    matches = [
        game_pk
        for game_pk, meta in game_meta.items()
        if normalize_name(meta.get("home_team_name") or "") == home
        and normalize_name(meta.get("away_team_name") or "") == away
    ]
    return matches[0] if len(matches) == 1 else None


def extract_rows(
    event: dict[str, Any],
    payload: dict[str, Any],
    game_pk: int,
    requested_markets: set[str],
    target_date: str,
    pitcher_by_name: dict[str, list[dict[str, Any]]],
    master_by_name: dict[str, list[dict[str, Any]]],
):
    batter_rows: list[dict[str, Any]] = []
    pitcher_rows: list[dict[str, Any]] = []
    event_id = str(payload.get("id") or event.get("id") or "")
    commence = payload.get("commence_time") or event.get("commence_time")
    home = payload.get("home_team") or event.get("home_team")
    away = payload.get("away_team") or event.get("away_team")
    fetched_at = datetime.now(timezone.utc).isoformat()

    for bookmaker in payload.get("bookmakers", []) or []:
        book_key = str(bookmaker.get("key") or bookmaker.get("title") or "unknown").lower()
        book_update = bookmaker.get("last_update")

        for market in bookmaker.get("markets", []) or []:
            market_key = str(market.get("key") or "")
            if market_key not in requested_markets:
                continue
            updated = market.get("last_update") or book_update

            for outcome in market.get("outcomes", []) or []:
                side = str(outcome.get("name") or "").strip().lower()
                if side not in {"over", "under"}:
                    continue
                try:
                    line = float(outcome.get("point"))
                    price = int(outcome.get("price"))
                except (TypeError, ValueError):
                    continue
                player = outcome.get("description") or outcome.get("participant") or outcome.get("player")
                if not player:
                    continue

                if market_key in BATTER_MARKETS:
                    market_name, target_line = BATTER_MARKETS[market_key]
                    if line != target_line:
                        continue
                    row = {
                        "odds_provider": "the_odds_api",
                        "book_name": book_key,
                        "provider_event_id": event_id,
                        "provider_market_id": market_key,
                        "game_pk": game_pk,
                        "game_date": target_date,
                        "commence_time_utc": commence,
                        "home_team": home,
                        "away_team": away,
                        "player_id": None,
                        "player_name_raw": str(player),
                        "market_key": market_key,
                        "market_name": market_name,
                        "line": line,
                        "outcome_name": side,
                        "american_odds": price,
                        "decimal_odds": None,
                        "odds_last_update": updated,
                        "fetched_at": fetched_at,
                        "raw_payload": {
                            "event": {"id": event_id, "commence_time": commence, "home_team": home, "away_team": away},
                            "bookmaker": {"key": bookmaker.get("key"), "title": bookmaker.get("title"), "last_update": book_update},
                            "market": {"key": market_key, "last_update": market.get("last_update")},
                            "outcome": outcome,
                        },
                    }
                    row["load_key"] = stable_batter_key(row)
                    batter_rows.append(row)

                elif market_key == PITCHER_MARKET:
                    norm = normalize_name(str(player))
                    matches = pitcher_by_name.get(norm, [])
                    resolved = matches[0] if len(matches) == 1 else {}
                    master_matches = master_by_name.get(norm, [])
                    master = master_matches[0] if len(master_matches) == 1 else {}
                    row = {
                        "odds_provider": "the_odds_api",
                        "book_name": book_key,
                        "provider_event_id": event_id,
                        "provider_market_id": PITCHER_MARKET,
                        "game_pk": resolved.get("game_pk") or game_pk,
                        "game_date": target_date,
                        "commence_time_utc": commence,
                        "home_team": home,
                        "away_team": away,
                        "pitcher_id": resolved.get("pitcher_id") or master.get("pitcher_id"),
                        "pitcher_name_raw": str(player),
                        "normalized_pitcher_name": norm,
                        "market_key": PITCHER_MARKET,
                        "market_name": "Pitcher Strikeouts",
                        "line": line,
                        "outcome_name": side,
                        "american_odds": price,
                        "decimal_odds": american_to_decimal(price),
                        "implied_probability": american_to_implied(price),
                        "odds_last_update": updated,
                        "fetched_at": fetched_at,
                        "raw_payload": {"outcome": outcome},
                    }
                    row["load_key"] = stable_pitcher_key(row)
                    pitcher_rows.append(row)

    return batter_rows, pitcher_rows


def main() -> int:
    sb = create_client(require("SUPABASE_URL"), require("SUPABASE_SERVICE_ROLE_KEY"))
    api_key = require("THE_ODDS_API_KEY")
    target_date = os.getenv("ODDS_TARGET_DATE") or datetime.now(ET).date().isoformat()
    regions = os.getenv("ODDS_REGIONS", "us")
    books_raw = (os.getenv("BOOKMAKERS") or "all").strip().lower()
    bookmakers = None if books_raw in {"all", "*", "any", ""} else ",".join(
        dict.fromkeys(x.strip() for x in books_raw.split(",") if x.strip())
    )
    max_events = int(os.getenv("MAX_EVENTS_PER_RUN", "20"))
    usage = Usage()

    required, game_meta, pitcher_by_name, master_by_name = load_prediction_requirements(sb, target_date)

    print(f"Odds target date: {target_date}")
    print(f"Games with model predictions: {len(required)}")
    for game_pk in sorted(required):
        print(f"  game_pk={game_pk}: {','.join(sorted(required[game_pk]))}")

    if not required:
        print("No model predictions found. No Odds API quota will be spent.")
        return 0

    events = get_json(
        f"/sports/{SPORT_KEY}/events",
        {"apiKey": api_key},
        usage,
    )
    now_utc = datetime.now(timezone.utc)
    candidates: list[tuple[dict[str, Any], int, set[str]]] = []
    unmatched: list[str] = []

    for event in events:
        if eastern_date(event.get("commence_time")) != target_date:
            continue
        start = parse_dt(event.get("commence_time"))
        if start and start <= now_utc:
            continue
        game_pk = match_provider_event(event, game_meta)
        if game_pk is None:
            unmatched.append(f"{event.get('away_team')} at {event.get('home_team')}")
            continue
        markets = required.get(game_pk)
        if markets:
            candidates.append((event, game_pk, markets))

    candidates = sorted(candidates, key=lambda x: x[0].get("commence_time") or "")[:max_events]
    if unmatched:
        print(f"Provider events not matched to eligible-game metadata: {unmatched}")

    batter_rows: list[dict[str, Any]] = []
    pitcher_rows: list[dict[str, Any]] = []
    estimated_market_credits = sum(len(markets) for _, _, markets in candidates)
    print(
        f"Will query {len(candidates)} predicted games; "
        f"requested game-market pairs={estimated_market_credits}."
    )

    for idx, (event, game_pk, markets) in enumerate(candidates, start=1):
        event_id = str(event.get("id") or "")
        label = f"{event.get('away_team')} at {event.get('home_team')}"
        market_csv = ",".join(sorted(markets))
        print(f"[{idx}/{len(candidates)}] {label} | game_pk={game_pk} | markets={market_csv}")

        params = {
            "apiKey": api_key,
            "markets": market_csv,
            "oddsFormat": "american",
        }
        # Explicit bookmakers take precedence over regions. Up to 10 bookmakers
        # count as one region-equivalent under The Odds API quota rules.
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = regions

        try:
            payload = get_json(
                f"/sports/{SPORT_KEY}/events/{event_id}/odds",
                params,
                usage,
            )
        except RuntimeError as exc:
            print(f"WARNING: {exc}", file=sys.stderr)
            continue

        b_rows, p_rows = extract_rows(
            event, payload, game_pk, markets, target_date, pitcher_by_name, master_by_name
        )
        batter_rows.extend(b_rows)
        pitcher_rows.extend(p_rows)
        time.sleep(0.10)

    requested_batter_markets = any(
        any(m in BATTER_MARKETS for m in markets) for _, _, markets in candidates
    )
    requested_pitcher = any(PITCHER_MARKET in markets for _, _, markets in candidates)

    if requested_batter_markets:
        snapshot = sb.rpc(
            "replace_mlb_batter_prop_market_odds_snapshot",
            {
                "p_odds_provider": "the_odds_api",
                "p_game_date": target_date,
                "p_rows": batter_rows,
            },
        ).execute()
        print("Atomic batter odds snapshot result:")
        print(json.dumps(snapshot.data, indent=2, default=str))

    if requested_pitcher and pitcher_rows:
        for i in range(0, len(pitcher_rows), 500):
            (
                sb.table("mlb_pitcher_k_market_odds")
                .upsert(pitcher_rows[i:i + 500], on_conflict="load_key")
                .execute()
            )

    print(f"Batter rows prepared: {len(batter_rows)}")
    print(f"Pitcher-K rows prepared: {len(pitcher_rows)}")
    print("Odds API usage:")
    print(json.dumps(usage.summary(), indent=2))

    # Preserve existing downstream validation visibility.
    try:
        paired = (
            sb.table("v_mlb_batter_prop_market_no_vig_best")
            .select("market_key", count="exact")
            .eq("game_date", target_date)
            .execute()
        )
        print(f"Batter no-vig rows: {paired.count or 0}")
    except Exception as exc:
        print(f"WARNING: batter no-vig validation failed: {exc}", file=sys.stderr)

    try:
        edges = (
            sb.table("v_mlb_pitcher_k_market_edges")
            .select("line", count="exact")
            .eq("game_date", target_date)
            .execute()
        )
        print(f"Pitcher-K market edge rows: {edges.count or 0}")
    except Exception as exc:
        print(f"WARNING: pitcher-K validation failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
