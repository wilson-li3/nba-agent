"""
Fetch every NBA game from the NBA API (stats.nba.com via nba_api) and load into PostgreSQL.
Run: python sync_games.py
Requires: DATABASE_URL in environment or .env (e.g. postgresql://user:pass@localhost:5432/nba)
"""

import os
import time

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from nba_api.stats.endpoints import leaguegamefinder

load_dotenv()

# Seasons to fetch: from first NBA season through current (format YYYY-YY)
FIRST_SEASON = "1946-47"
CURRENT_SEASON = "2024-25"  # bump this when new season starts
REQUEST_DELAY_SEC = 0.6  # avoid rate limits


def get_season_list() -> list[str]:
    """Generate list of season strings from FIRST_SEASON to CURRENT_SEASON."""
    start_year = int(FIRST_SEASON.split("-")[0])
    end_year = int(CURRENT_SEASON.split("-")[0])
    seasons = []
    for y in range(start_year, end_year + 1):
        seasons.append(f"{y}-{str(y + 1)[-2:]}")
    return seasons


def fetch_season_type(season: str, season_type: str) -> pd.DataFrame:
    """Fetch all game rows for one season and season type (e.g. Playoffs, PlayIn)."""
    finder = leaguegamefinder.LeagueGameFinder(
        season_nullable=season,
        season_type_nullable=season_type,
        player_or_team_abbreviation="T",
    )
    df = finder.get_data_frames()[0]
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def combine_team_rows_into_games(df: pd.DataFrame, season_type: str) -> pd.DataFrame:
    """
    Turn 2 rows per game (one per team) into 1 row per game with home/away.
    Home team is the one with ' vs. ' in MATCHUP; away has ' @ '.
    """
    if df is None or df.empty or len(df) == 0:
        return pd.DataFrame()

    # Merge each game row with its pair (same GAME_ID, different TEAM_ID)
    merged = df.merge(
        df,
        on=["GAME_ID", "GAME_DATE", "SEASON_ID"],
        suffixes=("_A", "_B"),
    )
    # Keep only pairs where the two teams differ
    merged = merged[merged["TEAM_ID_A"] != merged["TEAM_ID_B"]]

    # Keep one row per game: where team A is home (MATCHUP contains ' vs. ')
    home_mask = merged["MATCHUP_A"].str.contains(" vs. ", na=False)
    games = merged[home_mask].copy()

    if games.empty:
        return pd.DataFrame()

    # Build one row per game
    result = pd.DataFrame()
    result["game_id"] = games["GAME_ID"].values
    result["game_date"] = pd.to_datetime(games["GAME_DATE"]).dt.date
    result["season_id"] = games["SEASON_ID"].astype(str)
    result["season_type"] = season_type

    result["home_team_id"] = games["TEAM_ID_A"].values
    result["home_team_abbr"] = games["TEAM_ABBREVIATION_A"].values
    result["home_pts"] = games["PTS_A"].values
    result["home_wl"] = games["WL_A"].values

    result["away_team_id"] = games["TEAM_ID_B"].values
    result["away_team_abbr"] = games["TEAM_ABBREVIATION_B"].values
    result["away_pts"] = games["PTS_B"].values
    result["away_wl"] = games["WL_B"].values

    return result


def ensure_schema(conn) -> None:
    """Create games table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS games (
                game_id         TEXT PRIMARY KEY,
                game_date       DATE NOT NULL,
                season_id       TEXT NOT NULL,
                season_type     TEXT NOT NULL,
                home_team_id    BIGINT NOT NULL,
                home_team_abbr  TEXT NOT NULL,
                home_pts        INT NOT NULL,
                away_team_id    BIGINT NOT NULL,
                away_team_abbr  TEXT NOT NULL,
                away_pts        INT NOT NULL,
                home_wl         TEXT,
                away_wl         TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit()


def upsert_games(conn, df: pd.DataFrame) -> int:
    """Insert or update games. Returns number of rows affected."""
    if df is None or df.empty:
        return 0

    # Normalize columns to match table (only columns we create in schema)
    cols = [
        "game_id", "game_date", "season_id", "season_type",
        "home_team_id", "home_team_abbr", "home_pts",
        "away_team_id", "away_team_abbr", "away_pts",
        "home_wl", "away_wl",
    ]
    existing = [c for c in cols if c in df.columns]
    block = df[existing].copy()
    block["game_date"] = pd.to_datetime(block["game_date"]).dt.date

    with conn.cursor() as cur:
        for _, row in block.iterrows():
            cur.execute("""
                INSERT INTO games (
                    game_id, game_date, season_id, season_type,
                    home_team_id, home_team_abbr, home_pts,
                    away_team_id, away_team_abbr, away_pts,
                    home_wl, away_wl
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (game_id) DO UPDATE SET
                    game_date = EXCLUDED.game_date,
                    season_id = EXCLUDED.season_id,
                    season_type = EXCLUDED.season_type,
                    home_team_id = EXCLUDED.home_team_id,
                    home_team_abbr = EXCLUDED.home_team_abbr,
                    home_pts = EXCLUDED.home_pts,
                    away_team_id = EXCLUDED.away_team_id,
                    away_team_abbr = EXCLUDED.away_team_abbr,
                    away_pts = EXCLUDED.away_pts,
                    home_wl = EXCLUDED.home_wl,
                    away_wl = EXCLUDED.away_wl
            """, (
                str(row["game_id"]), row["game_date"], str(row["season_id"]), str(row["season_type"]),
                int(row["home_team_id"]), str(row["home_team_abbr"]), int(row["home_pts"]),
                int(row["away_team_id"]), str(row["away_team_abbr"]), int(row["away_pts"]),
                str(row["home_wl"]) if pd.notna(row["home_wl"]) else None,
                str(row["away_wl"]) if pd.notna(row["away_wl"]) else None,
            ))
        conn.commit()
        return len(block)


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Set DATABASE_URL (e.g. postgresql://user:password@localhost:5432/nba). "
            "You can put it in a .env file."
        )

    conn = psycopg2.connect(database_url)
    ensure_schema(conn)

    seasons = get_season_list()
    total_upserted = 0

    for i, season in enumerate(seasons):
        print(f"[{i+1}/{len(seasons)}] {season} ...", end=" ", flush=True)
        try:
            # Regular season
            time.sleep(REQUEST_DELAY_SEC)
            raw = fetch_season_type(season, "Regular Season")
            reg = combine_team_rows_into_games(raw, "Regular Season")
            if not reg.empty:
                n = upsert_games(conn, reg)
                total_upserted += n
                print(f"Regular: {n} games", end=" ")
            else:
                print("Regular: 0", end=" ")

            # Playoffs
            time.sleep(REQUEST_DELAY_SEC)
            raw_po = fetch_season_type(season, "Playoffs")
            po = combine_team_rows_into_games(raw_po, "Playoffs")
            if not po.empty:
                n_po = upsert_games(conn, po)
                total_upserted += n_po
                print(f"Playoffs: {n_po}", end=" ")
            # Play-In (exists from 2020-21 onward)
            time.sleep(REQUEST_DELAY_SEC)
            raw_pi = fetch_season_type(season, "PlayIn")
            pi = combine_team_rows_into_games(raw_pi, "PlayIn")
            if not pi.empty:
                n_pi = upsert_games(conn, pi)
                total_upserted += n_pi
                print(f"PlayIn: {n_pi}", end="")
            print()
        except Exception as e:
            print(f"Error: {e}")
            continue

    conn.close()
    print(f"Done. Total rows upserted this run: {total_upserted}")


if __name__ == "__main__":
    main()
