#!/usr/bin/env python3
"""
Phase 1 all-MLB loader for MLB Hitter Lab.

Loads:
  1) Active MLB hitters into public.mlb_players
  2) Active/probable pitchers into public.mlb_pitchers
  3) Recent batting game logs for all MLB teams into public.mlb_player_batting_game_logs
  4) Season batting splits for active hitters into public.mlb_player_batting_splits
  5) Today/tomorrow all-MLB team-vs-starter matchups into public.mlb_daily_team_matchups
  6) Probable starter season stats, splits, and game logs into pitcher tables

Required env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY preferred, or SUPABASE_KEY fallback
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from supabase import create_client

MLB_API = "https://statsapi.mlb.com/api/v1"
SPORT_ID = 1
DEFAULT_SEASON = 2026
POSITION_PITCHERS = {"P"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load all-MLB phase 1 data into Supabase.")
    p.add_argument("--season", type=int, default=DEFAULT_SEASON)
    p.add_argument("--date", default=dt.date.today().isoformat(), help="YYYY-MM-DD date for daily matchups.")
    p.add_argument("--days-back", type=int, default=21, help="Recent game-log lookback.")
    p.add_argument("--also-tomorrow", action="store_true", default=True)
    p.add_argument("--sleep", type=float, default=0.08, help="Sleep between MLB API calls.")
    p.add_argument("--max-teams", type=int, default=0, help="Debug only: limit teams.")
    p.add_argument("--max-hitters", type=int, default=0, help="Debug only: limit hitters for splits.")
    p.add_argument("--skip-splits", action="store_true")
    p.add_argument("--skip-pitchers", action="store_true")
    return p.parse_args()


def api_get(path: str, params: Optional[Dict[str, Any]] = None, sleep_seconds: float = 0.05) -> Dict[str, Any]:
    url = f"{MLB_API}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "mlb-hitter-lab-loader/1.0"})
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if sleep_seconds:
        time.sleep(sleep_seconds)
    return data


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def as_int(v: Any, default: int = 0) -> int:
    if v in (None, "", "--"):
        return default
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return default


def as_float(v: Any) -> Optional[float]:
    if v in (None, "", "--"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def as_rate(v: Any) -> Optional[float]:
    if v in (None, "", "--"):
        return None
    s = str(v).strip()
    if s.startswith("."):
        s = "0" + s
    return as_float(s)


def parse_ip(v: Any) -> Optional[float]:
    if v in (None, "", "--"):
        return None
    s = str(v)
    if "." not in s:
        return as_float(s)
    whole, frac = s.split(".", 1)
    outs = as_int(frac[:1], 0)
    if outs not in (0, 1, 2):
        return as_float(s)
    return as_int(whole, 0) + outs / 3.0


def get_pitcher_hand(pitcher_id: Any, sleep: float = 0.05) -> Optional[str]:
    if not pitcher_id:
        return None
    try:
        data = api_get(f"/people/{pitcher_id}", {}, sleep)
        people = data.get("people") or []
        if not people:
            return None
        hand = (people[0].get("pitchHand") or {}).get("code")
        return hand if hand in ("L", "R") else None
    except Exception:
        return None


def chunked(items: List[Dict[str, Any]], size: int = 500) -> Iterable[List[Dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def dedupe_rows_by_conflict(rows: List[Dict[str, Any]], on_conflict: Optional[str]) -> List[Dict[str, Any]]:
    """
    Postgres cannot process the same ON CONFLICT key twice in one upsert command.
    Keep the latest row per conflict key before chunking/upserting.
    """
    if not rows or not on_conflict:
        return rows

    keys = [key.strip() for key in on_conflict.split(",") if key.strip()]
    if not keys:
        return rows

    deduped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    missing_key_rows: List[Dict[str, Any]] = []

    for row in rows:
        try:
            conflict_key = tuple(row[key] for key in keys)
        except KeyError:
            missing_key_rows.append(row)
            continue

        if any(value is None for value in conflict_key):
            missing_key_rows.append(row)
            continue

        deduped[conflict_key] = row

    return list(deduped.values()) + missing_key_rows


def strip_null_handedness_fields(table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Supabase upsert updates columns explicitly set to null.
    Avoid wiping known handedness/split-critical fields when an upstream API response omits them.
    """
    protected_fields_by_table = {
        "mlb_players": ["bats", "throws"],
        "mlb_pitchers": ["throws"],
        "mlb_daily_team_matchups": ["probable_pitcher_throws"],
        "mlb_daily_matchups": ["probable_pitcher_throws"],
    }

    protected_fields = protected_fields_by_table.get(table, [])
    if not protected_fields:
        return rows

    cleaned = []
    for row in rows:
        next_row = dict(row)
        for field in protected_fields:
            if next_row.get(field) is None:
                next_row.pop(field, None)
        cleaned.append(next_row)

    return cleaned


def upsert_rows(client, table: str, rows: List[Dict[str, Any]], on_conflict: Optional[str] = None) -> None:
    if not rows:
        print(f"{table}: no rows")
        return

    original_count = len(rows)
    rows = dedupe_rows_by_conflict(rows, on_conflict)

    if len(rows) != original_count:
        print(f"{table}: deduped {original_count - len(rows)} duplicate rows before upsert")

    rows = strip_null_handedness_fields(table, rows)

    print(f"{table}: upserting {len(rows)} rows")
    for batch in chunked(rows):
        q = client.table(table).upsert(batch, on_conflict=on_conflict) if on_conflict else client.table(table).upsert(batch)
        q.execute()


def get_client():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY/SUPABASE_KEY")
    return create_client(url, key)


def get_teams(season: int, sleep: float) -> List[Dict[str, Any]]:
    data = api_get("/teams", {"sportId": SPORT_ID, "season": season, "activeStatus": "Y"}, sleep)
    return [t for t in data.get("teams", []) if t.get("sport", {}).get("id") == SPORT_ID]


def get_roster(team_id: int, season: int, sleep: float) -> List[Dict[str, Any]]:
    data = api_get(f"/teams/{team_id}/roster", {"rosterType": "active", "season": season, "hydrate": "person"}, sleep)
    return data.get("roster", [])


def person_row(person: Dict[str, Any], team: Dict[str, Any]) -> Dict[str, Any]:
    pos = person.get("primaryPosition") or {}
    return {
        "player_id": person.get("id"),
        "full_name": person.get("fullName") or "Unknown Player",
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "active": True,
        "primary_position": pos.get("abbreviation") or pos.get("code"),
        "bats": (person.get("batSide") or {}).get("code"),
        "throws": (person.get("pitchHand") or {}).get("code"),
        "mlb_link": person.get("link"),
        "updated_at": now_utc(),
    }


def load_active_players(client, teams: List[Dict[str, Any]], season: int, sleep: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    hitters: Dict[int, Dict[str, Any]] = {}
    pitchers: Dict[int, Dict[str, Any]] = {}
    for team in teams:
        print(f"Loading active roster: {team.get('name')}")
        for entry in get_roster(team["id"], season, sleep):
            person = entry.get("person") or {}
            row = person_row(person, team)
            player_id = row.get("player_id")
            if not player_id:
                continue
            if row.get("primary_position") in POSITION_PITCHERS:
                pitchers[player_id] = {
                    "pitcher_id": row["player_id"],
                    "full_name": row["full_name"],
                    "team_id": row["team_id"],
                    "team_name": row["team_name"],
                    "active": row["active"],
                    "primary_position": row["primary_position"],
                    "throws": row["throws"],
                    "mlb_link": row["mlb_link"],
                    "updated_at": row["updated_at"],
                }
            else:
                hitters[player_id] = row
    upsert_rows(client, "mlb_players", list(hitters.values()), on_conflict="player_id")
    upsert_rows(client, "mlb_pitchers", list(pitchers.values()), on_conflict="pitcher_id")
    return list(hitters.values()), list(pitchers.values())


def load_daily_team_matchups(client, season: int, date_text: str, sleep: float, also_tomorrow: bool = True) -> List[int]:
    dates = [dt.date.fromisoformat(date_text)]
    if also_tomorrow:
        dates.append(dates[0] + dt.timedelta(days=1))
    rows: List[Dict[str, Any]] = []
    pitcher_ids: List[int] = []
    for game_date in dates:
        data = api_get("/schedule", {"sportId": SPORT_ID, "date": game_date.isoformat(), "hydrate": "probablePitcher,team,venue"}, sleep)
        for day in data.get("dates", []):
            for game in day.get("games", []):
                teams = game.get("teams") or {}
                away = teams.get("away") or {}
                home = teams.get("home") or {}
                away_team = away.get("team") or {}
                home_team = home.get("team") or {}
                away_pitcher = away.get("probablePitcher") or {}
                home_pitcher = home.get("probablePitcher") or {}

                away_pitcher_throws = (away_pitcher.get("pitchHand") or {}).get("code") or get_pitcher_hand(away_pitcher.get("id"), sleep)
                home_pitcher_throws = (home_pitcher.get("pitchHand") or {}).get("code") or get_pitcher_hand(home_pitcher.get("id"), sleep)
                base = {
                    "game_pk": game.get("gamePk"),
                    "game_date": game_date.isoformat(),
                    "season": season,
                    "game_type": game.get("gameType"),
                    "game_status": (game.get("status") or {}).get("detailedState"),
                    "venue_name": (game.get("venue") or {}).get("name"),
                    "game_time_utc": game.get("gameDate"),
                    "updated_at": now_utc(),
                }
                rows.append({**base, "batting_team_id": away_team.get("id"), "batting_team_name": away_team.get("name"), "pitching_team_id": home_team.get("id"), "pitching_team_name": home_team.get("name"), "home_away": "away", "probable_pitcher_id": home_pitcher.get("id"), "probable_pitcher_name": home_pitcher.get("fullName"), "probable_pitcher_throws": home_pitcher_throws})
                rows.append({**base, "batting_team_id": home_team.get("id"), "batting_team_name": home_team.get("name"), "pitching_team_id": away_team.get("id"), "pitching_team_name": away_team.get("name"), "home_away": "home", "probable_pitcher_id": away_pitcher.get("id"), "probable_pitcher_name": away_pitcher.get("fullName"), "probable_pitcher_throws": away_pitcher_throws})
                for pitcher in (away_pitcher, home_pitcher):
                    if pitcher.get("id"):
                        pitcher_ids.append(int(pitcher["id"]))
    rows = [r for r in rows if r.get("game_pk") and r.get("batting_team_id")]
    upsert_rows(client, "mlb_daily_team_matchups", rows, on_conflict="game_pk,batting_team_id")
    pitcher_rows = {}
    for r in rows:
        pid = r.get("probable_pitcher_id")
        if not pid:
            continue
        pitcher_rows[pid] = {"pitcher_id": pid, "full_name": r.get("probable_pitcher_name") or "Unknown Pitcher", "team_id": r.get("pitching_team_id"), "team_name": r.get("pitching_team_name"), "active": True, "primary_position": "P", "throws": r.get("probable_pitcher_throws"), "mlb_link": f"/api/v1/people/{pid}", "updated_at": now_utc()}
    upsert_rows(client, "mlb_pitchers", list(pitcher_rows.values()), on_conflict="pitcher_id")
    return sorted(set(pitcher_ids))


def sync_reds_daily_matchups(client) -> None:
    """
    Keep the legacy Reds matchup table populated from the all-MLB team matchup table.
    The Reds Hit Board still reads public.mlb_daily_matchups, while the MLB board reads
    public.mlb_daily_team_matchups.
    """
    rows = (
        client.table("mlb_daily_team_matchups")
        .select("*")
        .eq("batting_team_id", 113)
        .execute()
        .data
        or []
    )

    mapped = []
    for row in rows:
        mapped.append({
            "game_pk": row.get("game_pk"),
            "game_date": row.get("game_date"),
            "season": row.get("season"),
            "game_type": row.get("game_type"),
            "is_prediction_eligible": row.get("is_prediction_eligible"),
            "ineligibility_reason": row.get("ineligibility_reason"),
            "reds_team_id": row.get("batting_team_id"),
            "opponent_team_id": row.get("pitching_team_id"),
            "opponent_team_name": row.get("pitching_team_name"),
            "home_away": row.get("home_away"),
            "probable_pitcher_id": row.get("probable_pitcher_id"),
            "probable_pitcher_name": row.get("probable_pitcher_name"),
            "probable_pitcher_throws": row.get("probable_pitcher_throws"),
            "game_status": row.get("game_status"),
            "venue_name": row.get("venue_name"),
            "game_time_utc": row.get("game_time_utc"),
            "updated_at": row.get("updated_at"),
        })

    upsert_rows(client, "mlb_daily_matchups", mapped, on_conflict="game_pk")


def load_batting_game_logs(client, season: int, start_date: dt.date, end_date: dt.date, sleep: float) -> None:
    data = api_get("/schedule", {"sportId": SPORT_ID, "startDate": start_date.isoformat(), "endDate": end_date.isoformat(), "gameTypes": "R,F,D,L,W"}, sleep)
    games = []
    for day in data.get("dates", []):
        official_date = day.get("date")
        for game in day.get("games", []):
            game["_official_schedule_date"] = game.get("officialDate") or official_date
            games.append(game)

    rows: List[Dict[str, Any]] = []
    player_rows: Dict[int, Dict[str, Any]] = {}
    print(f"Loading boxscores for {len(games)} games from {start_date} to {end_date}")
    for game in games:
        detailed = (game.get("status") or {}).get("detailedState")
        coded = (game.get("status") or {}).get("codedGameState")
        if detailed not in ("Final", "Game Over") and coded not in ("F", "O"):
            continue
        game_pk = game.get("gamePk")
        # Use MLB official/local schedule date, not UTC gameDate.
        # UTC gameDate can shift night games into the next calendar day.
        game_date = game.get("_official_schedule_date") or (game.get("gameDate") or "")[:10]
        box = api_get(f"/game/{game_pk}/boxscore", {}, sleep)
        for side in ("away", "home"):
            team_box = (box.get("teams") or {}).get(side) or {}
            team = team_box.get("team") or {}
            opp_side = "home" if side == "away" else "away"
            opp_team = ((box.get("teams") or {}).get(opp_side) or {}).get("team") or {}
            for _, p in (team_box.get("players") or {}).items():
                person = p.get("person") or {}
                stats = ((p.get("stats") or {}).get("batting") or {})
                player_id = person.get("id")
                if not player_id or not stats:
                    continue
                ab = as_int(stats.get("atBats"))
                pa = as_int(stats.get("plateAppearances"))
                if ab == 0 and pa == 0:
                    continue
                pos = (p.get("position") or {}).get("abbreviation")
                player_rows[player_id] = {"player_id": player_id, "full_name": person.get("fullName") or "Unknown Player", "team_id": team.get("id"), "team_name": team.get("name"), "active": True, "primary_position": pos, "mlb_link": person.get("link"), "updated_at": now_utc()}
                batting_order = p.get("battingOrder")
                rows.append({"player_id": player_id, "game_pk": game_pk, "game_date": game_date, "team_id": team.get("id"), "opponent_team_id": opp_team.get("id"), "opponent_team_name": opp_team.get("name"), "home_away": side, "batting_order": as_int(batting_order) if batting_order else None, "at_bats": ab, "runs": as_int(stats.get("runs")), "hits": as_int(stats.get("hits")), "doubles": as_int(stats.get("doubles")), "triples": as_int(stats.get("triples")), "home_runs": as_int(stats.get("homeRuns")), "rbi": as_int(stats.get("rbi")), "walks": as_int(stats.get("baseOnBalls")), "strikeouts": as_int(stats.get("strikeOuts")), "stolen_bases": as_int(stats.get("stolenBases")), "caught_stealing": as_int(stats.get("caughtStealing")), "plate_appearances": pa, "updated_at": now_utc()})
    # Boxscore rows intentionally omit bats/throws so they do not overwrite roster/person handedness.
    upsert_rows(client, "mlb_players", list(player_rows.values()), on_conflict="player_id")
    upsert_rows(client, "mlb_player_batting_game_logs", rows, on_conflict="player_id,game_pk")

def load_hitter_splits(client, hitters: List[Dict[str, Any]], season: int, sleep: float, max_hitters: int = 0) -> None:
    sit_map = {"vl": ("pitcher_hand", "LHP"), "vr": ("pitcher_hand", "RHP"), "h": ("venue", "home"), "a": ("venue", "away"), "d": ("time_of_day", "day"), "n": ("time_of_day", "night")}
    target = hitters[:max_hitters] if max_hitters else hitters
    rows: List[Dict[str, Any]] = []
    print(f"Loading hitting splits for {len(target)} hitters")
    for idx, player in enumerate(target, start=1):
        if idx % 25 == 0:
            print(f"  hitter splits {idx}/{len(target)}")
        try:
            data = api_get(f"/people/{player['player_id']}/stats", {"stats": "statSplits", "group": "hitting", "season": season, "sitCodes": ",".join(sit_map.keys())}, sleep)
            splits = [s for stat in data.get("stats", []) for s in stat.get("splits", [])]
            for split in splits:
                code = (split.get("split") or {}).get("code")
                if code not in sit_map:
                    continue
                split_type, split_value = sit_map[code]
                s = split.get("stat") or {}
                rows.append({"season": season, "player_id": player["player_id"], "team_id": player.get("team_id"), "split_type": split_type, "split_value": split_value, "games": as_int(s.get("gamesPlayed")), "at_bats": as_int(s.get("atBats")), "runs": as_int(s.get("runs")), "hits": as_int(s.get("hits")), "doubles": as_int(s.get("doubles")), "triples": as_int(s.get("triples")), "home_runs": as_int(s.get("homeRuns")), "rbi": as_int(s.get("rbi")), "walks": as_int(s.get("baseOnBalls")), "strikeouts": as_int(s.get("strikeOuts")), "stolen_bases": as_int(s.get("stolenBases")), "caught_stealing": as_int(s.get("caughtStealing")), "plate_appearances": as_int(s.get("plateAppearances")), "batting_average": as_rate(s.get("avg")), "on_base_percentage": as_rate(s.get("obp")), "slugging_percentage": as_rate(s.get("slg")), "ops": as_rate(s.get("ops")), "updated_at": now_utc()})
        except Exception as exc:
            print(f"  split load failed for {player.get('full_name')} ({player.get('player_id')}): {exc}")
    upsert_rows(client, "mlb_player_batting_splits", rows, on_conflict="season,player_id,split_type,split_value")


def load_probable_pitcher_data(client, pitcher_ids: List[int], season: int, sleep: float) -> None:
    season_rows, split_rows, game_rows = [], [], []
    print(f"Loading pitcher data for {len(pitcher_ids)} probable starters")
    for pid in pitcher_ids:
        try:
            data = api_get(f"/people/{pid}/stats", {"stats": "season", "group": "pitching", "season": season}, sleep)
            splits = [s for stat in data.get("stats", []) for s in stat.get("splits", [])]
            if splits:
                split = splits[0]
                s = split.get("stat") or {}
                team = split.get("team") or {}
                season_rows.append({"season": season, "pitcher_id": pid, "team_id": team.get("id"), "games": as_int(s.get("gamesPlayed")), "games_started": as_int(s.get("gamesStarted")), "innings_pitched": parse_ip(s.get("inningsPitched")), "era": as_rate(s.get("era")), "whip": as_rate(s.get("whip")), "hits_allowed": as_int(s.get("hits")), "runs_allowed": as_int(s.get("runs")), "earned_runs": as_int(s.get("earnedRuns")), "walks": as_int(s.get("baseOnBalls")), "strikeouts": as_int(s.get("strikeOuts")), "home_runs_allowed": as_int(s.get("homeRuns")), "batting_average_against": as_rate(s.get("avg")), "obp_against": as_rate(s.get("obp")), "slg_against": as_rate(s.get("slg")), "ops_against": as_rate(s.get("ops")), "updated_at": now_utc()})
        except Exception as exc:
            print(f"  pitcher season failed for {pid}: {exc}")
        try:
            data = api_get(f"/people/{pid}/stats", {"stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "vl,vr"}, sleep)
            splits = [s for stat in data.get("stats", []) for s in stat.get("splits", [])]
            for split in splits:
                code = (split.get("split") or {}).get("code")
                if code == "vl":
                    split_value = "LHB"
                elif code == "vr":
                    split_value = "RHB"
                else:
                    continue
                s = split.get("stat") or {}
                team = split.get("team") or {}
                avg_against = as_rate(s.get("avg"))
                hits_allowed = as_int(s.get("hits"))
                ab_against = round(hits_allowed / avg_against) if avg_against and avg_against > 0 else None
                split_rows.append({"season": season, "pitcher_id": pid, "team_id": team.get("id"), "split_type": "batter_hand", "split_value": split_value, "games": as_int(s.get("gamesPlayed")), "games_started": as_int(s.get("gamesStarted")), "innings_pitched": parse_ip(s.get("inningsPitched")), "hits_allowed": hits_allowed, "runs_allowed": as_int(s.get("runs")), "earned_runs": as_int(s.get("earnedRuns")), "walks": as_int(s.get("baseOnBalls")), "strikeouts": as_int(s.get("strikeOuts")), "home_runs_allowed": as_int(s.get("homeRuns")), "batting_average_against": avg_against, "obp_against": as_rate(s.get("obp")), "slg_against": as_rate(s.get("slg")), "ops_against": as_rate(s.get("ops")), "at_bats_against": ab_against, "updated_at": now_utc()})
        except Exception as exc:
            print(f"  pitcher splits failed for {pid}: {exc}")
        try:
            data = api_get(f"/people/{pid}/stats", {"stats": "gameLog", "group": "pitching", "season": season}, sleep)
            splits = [s for stat in data.get("stats", []) for s in stat.get("splits", [])]
            for split in splits:
                s = split.get("stat") or {}
                team = split.get("team") or {}
                opponent = split.get("opponent") or {}
                game = split.get("game") or {}
                game_pk = game.get("gamePk")
                if not game_pk:
                    continue
                game_rows.append({"pitcher_id": pid, "game_pk": game_pk, "game_date": split.get("date"), "season": season, "team_id": team.get("id"), "opponent_team_id": opponent.get("id"), "opponent_team_name": opponent.get("name"), "home_away": "home" if split.get("isHome") else "away", "is_start": as_int(s.get("gamesStarted")) > 0, "innings_pitched": parse_ip(s.get("inningsPitched")), "hits_allowed": as_int(s.get("hits")), "runs_allowed": as_int(s.get("runs")), "earned_runs": as_int(s.get("earnedRuns")), "walks": as_int(s.get("baseOnBalls")), "strikeouts": as_int(s.get("strikeOuts")), "home_runs_allowed": as_int(s.get("homeRuns")), "pitches": as_int(s.get("numberOfPitches")), "strikes": as_int(s.get("strikes")), "updated_at": now_utc()})
        except Exception as exc:
            print(f"  pitcher game logs failed for {pid}: {exc}")
    upsert_rows(client, "mlb_pitcher_season_stats", season_rows, on_conflict="season,pitcher_id")
    upsert_rows(client, "mlb_pitcher_splits", split_rows, on_conflict="season,pitcher_id,split_type,split_value")
    upsert_rows(client, "mlb_pitcher_game_logs", game_rows, on_conflict="pitcher_id,game_pk")


def main() -> None:
    args = parse_args()
    client = get_client()
    date_value = dt.date.fromisoformat(args.date)
    start_date = date_value - dt.timedelta(days=args.days_back)
    print("Starting all-MLB Phase 1 loader")
    print(f"Season={args.season} date={date_value} window={start_date}..{date_value}")
    teams = get_teams(args.season, args.sleep)
    if args.max_teams:
        teams = teams[:args.max_teams]
    print(f"Teams found: {len(teams)}")
    hitters, _ = load_active_players(client, teams, args.season, args.sleep)
    probable_pitcher_ids = load_daily_team_matchups(client, args.season, args.date, args.sleep, args.also_tomorrow)
    sync_reds_daily_matchups(client)
    load_batting_game_logs(client, args.season, start_date, date_value, args.sleep)
    if not args.skip_splits:
        load_hitter_splits(client, hitters, args.season, args.sleep, args.max_hitters)
    if not args.skip_pitchers:
        load_probable_pitcher_data(client, probable_pitcher_ids, args.season, args.sleep)
    print("All-MLB Phase 1 loader complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
