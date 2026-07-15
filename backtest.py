"""Backtest the prop prediction engine against historical box scores.

Walks each season chronologically, predicting every eligible player-game
prop using only data available before tip-off, then scores the predictions
against what actually happened.

Usage:
    python backtest.py --seasons 2023-24,2024-25
    python backtest.py --seasons 2022-23 --mode market
    python backtest.py --seasons 2021-22,2022-23 --fit-calibration

Modes:
    thresholds  score the app's fixed prop thresholds (20+ pts, 8+ reb, ...)
    market      synthetic book-style lines at the player's trailing median
                (x.5 lines, ~50/50), the hardest test of the model

Reports Brier score, log loss, a calibration table, and simulated ROI at
-110 odds for high-confidence picks.
"""

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import date

import psycopg2
from dotenv import load_dotenv

from app.services.prediction_engine import ENGINE_PARAMS, predict

load_dotenv()

THRESHOLDS = {
    "pts": [15, 20, 25, 30],
    "reb": [6, 8, 10],
    "ast": [4, 6, 8],
    "fg3m": [2, 3, 4],
    "pra": [30, 40],
}

STATS = ["pts", "reb", "ast", "fg3m", "pra"]


def load_season(conn, season_id: str) -> list[dict]:
    """Load one season's player-game rows, sorted by date."""
    sql = """
    SELECT s.player_id, s.game_id, s.game_date, s.team_abbr, s.matchup,
           s.min AS minutes, s.pts, s.reb, s.ast, s.fg3m
    FROM player_game_stats s
    WHERE s.season_id = %s AND s.min > 0
    ORDER BY s.game_date, s.game_id;
    """
    cur = conn.cursor()
    cur.execute(sql, (season_id,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["pra"] = r["pts"] + r["reb"] + r["ast"]
        m = r["matchup"]
        r["is_home"] = " vs. " in m
        r["opponent"] = m.split(" @ ")[-1] if " @ " in m else m.split(" vs. ")[-1]
    return rows


def build_team_context(rows: list[dict]) -> tuple[dict, dict]:
    """Per-game team totals -> (allowed_by_team_by_date, team_game_dates).

    allowed[(team, game_id)] = {stat: opponent total} for that game.
    dates[team] = sorted list of game dates.
    """
    totals: dict[tuple, dict] = defaultdict(lambda: defaultdict(float))
    game_teams: dict[str, set] = defaultdict(set)
    game_date_map: dict[tuple, date] = {}
    for r in rows:
        key = (r["game_id"], r["team_abbr"])
        for s in ["pts", "reb", "ast", "fg3m"]:
            totals[key][s] += r[s]
        game_teams[r["game_id"]].add(r["team_abbr"])
        game_date_map[key] = r["game_date"]

    allowed: dict[tuple, dict] = {}
    team_dates: dict[str, list] = defaultdict(list)
    for (gid, team), stat_totals in totals.items():
        others = game_teams[gid] - {team}
        if len(others) != 1:
            continue
        opp = next(iter(others))
        allowed[(gid, team)] = dict(totals[(gid, opp)])
        team_dates[team].append(game_date_map[(gid, team)])
    for t in team_dates:
        team_dates[t].sort()
    return allowed, team_dates


class RollingDefense:
    """Season-to-date stat allowed per team, and league average."""

    def __init__(self):
        self.sums = defaultdict(lambda: defaultdict(float))
        self.counts = defaultdict(int)
        self.league_sums = defaultdict(float)
        self.league_count = 0

    def factor(self, team: str, stat: str) -> float:
        s = "pts" if stat == "pra" else stat
        if self.counts[team] < 5 or self.league_count < 50:
            return 1.0
        team_avg = self.sums[team][s] / self.counts[team]
        league_avg = self.league_sums[s] / self.league_count
        return team_avg / league_avg if league_avg > 0 else 1.0

    def update(self, team: str, allowed: dict):
        for s, v in allowed.items():
            self.sums[team][s] += v
            self.league_sums[s] += v
        self.counts[team] += 1
        self.league_count += 1


def run_backtest(conn, season_id: str, mode: str, params: dict | None = None,
                 collect: list | None = None, rows: list[dict] | None = None) -> list[dict]:
    """Run one season. Returns list of prediction records."""
    if rows is None:
        rows = load_season(conn, season_id)
    allowed_map, team_dates = build_team_context(rows)

    history = defaultdict(lambda: {s: [] for s in STATS} | {"minutes": [], "ha": defaultdict(list)})
    defense = RollingDefense()
    seen_team_games: set = set()
    last_team_date: dict[str, date] = {}
    preds: list[dict] = collect if collect is not None else []

    current_date = None
    pending_defense: list[tuple] = []

    for r in rows:
        if r["game_date"] != current_date:
            # Day rolled over — fold yesterday's games into defense ratings
            for team, al in pending_defense:
                defense.update(team, al)
            pending_defense = []
            current_date = r["game_date"]

        pid = r["player_id"]
        h = history[pid]
        team = r["team_abbr"]

        days_rest = None
        if team in last_team_date and last_team_date[team] < r["game_date"]:
            days_rest = (r["game_date"] - last_team_date[team]).days

        for stat in STATS:
            values = h[stat]
            if len(values) < ENGINE_PARAMS["min_games"]:
                continue

            home_vals = h["ha"][f"{stat}_home"]
            away_vals = h["ha"][f"{stat}_away"]
            ha_diff = None
            if len(home_vals) >= 5 and len(away_vals) >= 5:
                ha_diff = sum(home_vals) / len(home_vals) - sum(away_vals) / len(away_vals)

            opp_factor = defense.factor(r["opponent"], stat)

            if mode == "market":
                window = sorted(values[:20])
                med = window[len(window) // 2] if len(window) % 2 else (
                    (window[len(window) // 2 - 1] + window[len(window) // 2]) / 2)
                lines = [math.floor(med) + 0.5]
            else:
                lines = THRESHOLDS[stat]

            for line in lines:
                p = predict(
                    values, h["minutes"], line, stat=stat,
                    opp_factor=opp_factor, is_home=r["is_home"],
                    home_away_diff=ha_diff, days_rest=days_rest,
                    params=params,
                )
                if not p.eligible:
                    continue
                # Skip degenerate props far from the player's range
                if mode == "thresholds" and (p.prob_raw < 0.05 or p.prob_raw > 0.97):
                    continue
                preds.append({
                    "season": season_id,
                    "date": r["game_date"],
                    "player_id": pid,
                    "stat": stat,
                    "line": line,
                    "prob": p.prob,
                    "prob_raw": p.prob_raw,
                    "outcome": 1 if r[stat] >= line else 0,
                })

        # ── After predicting, record the game into history ──
        for stat in STATS:
            h[stat].insert(0, float(r[stat]))
            h["ha"][f"{stat}_{'home' if r['is_home'] else 'away'}"].append(float(r[stat]))
        h["minutes"].insert(0, float(r["minutes"]))

        tg_key = (r["game_id"], team)
        if tg_key not in seen_team_games:
            seen_team_games.add(tg_key)
            last_team_date[team] = r["game_date"]
            if tg_key in allowed_map:
                pending_defense.append((team, allowed_map[tg_key]))

    return preds


# ── Metrics ─────────────────────────────────────────────────────────────────

def brier(preds: list[dict], key: str = "prob") -> float:
    return sum((p[key] - p["outcome"]) ** 2 for p in preds) / len(preds)


def log_loss(preds: list[dict], key: str = "prob") -> float:
    eps = 1e-6
    total = 0.0
    for p in preds:
        q = min(max(p[key], eps), 1 - eps)
        total += -(p["outcome"] * math.log(q) + (1 - p["outcome"]) * math.log(1 - q))
    return total / len(preds)


def calibration_table(preds: list[dict], key: str = "prob") -> list[dict]:
    buckets = defaultdict(list)
    for p in preds:
        buckets[min(int(p[key] * 10), 9)].append(p)
    table = []
    for b in sorted(buckets):
        group = buckets[b]
        table.append({
            "bucket": f"{b*10}-{b*10+10}%",
            "n": len(group),
            "predicted": sum(p[key] for p in group) / len(group),
            "actual": sum(p["outcome"] for p in group) / len(group),
        })
    return table


def roi_at_110(preds: list[dict], min_prob: float, key: str = "prob") -> dict:
    """Simulated flat-stake ROI betting overs at -110 when prob >= min_prob."""
    bets = [p for p in preds if p[key] >= min_prob]
    if not bets:
        return {"bets": 0, "hit_rate": 0.0, "roi": 0.0}
    wins = sum(p["outcome"] for p in bets)
    profit = wins * (100 / 110) - (len(bets) - wins)
    return {"bets": len(bets), "hit_rate": wins / len(bets), "roi": profit / len(bets)}


def fit_calibration(preds: list[dict]) -> tuple[float, float]:
    """Logistic regression of outcome on logit(prob_raw) via Newton's method."""
    xs = [math.log(min(max(p["prob_raw"], 1e-6), 1 - 1e-6) /
                   (1 - min(max(p["prob_raw"], 1e-6), 1 - 1e-6))) for p in preds]
    ys = [p["outcome"] for p in preds]
    a, b = 0.0, 1.0
    for _ in range(25):
        g_a = g_b = 0.0
        h_aa = h_ab = h_bb = 0.0
        for x, y in zip(xs, ys):
            mu = 1.0 / (1.0 + math.exp(-(a + b * x)))
            w = mu * (1 - mu)
            g_a += mu - y
            g_b += (mu - y) * x
            h_aa += w
            h_ab += w * x
            h_bb += w * x * x
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a -= da
        b -= db
        if abs(da) < 1e-8 and abs(db) < 1e-8:
            break
    return a, b


def print_report(preds: list[dict], label: str, key: str = "prob"):
    print(f"\n{'='*62}")
    print(f"  {label} — {len(preds):,} predictions")
    print(f"{'='*62}")
    print(f"  Brier score : {brier(preds, key):.4f}   (0.25 = coin flip, lower is better)")
    print(f"  Log loss    : {log_loss(preds, key):.4f}")
    base = sum(p['outcome'] for p in preds) / len(preds)
    print(f"  Base rate   : {base:.3f}")
    print(f"\n  Calibration (predicted vs actual):")
    print(f"  {'bucket':>10} {'n':>8} {'predicted':>10} {'actual':>8} {'gap':>7}")
    for row in calibration_table(preds, key):
        gap = row["actual"] - row["predicted"]
        print(f"  {row['bucket']:>10} {row['n']:>8,} {row['predicted']:>10.3f} {row['actual']:>8.3f} {gap:>+7.3f}")
    print(f"\n  Simulated flat-stake ROI at -110 (betting overs):")
    for t in [0.60, 0.65, 0.70, 0.75, 0.80]:
        r = roi_at_110(preds, t, key)
        print(f"    p >= {t:.2f}: {r['bets']:>7,} bets, hit rate {r['hit_rate']:.3f}, ROI {r['roi']:>+.3f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seasons", default="2023-24,2024-25")
    ap.add_argument("--mode", choices=["thresholds", "market"], default="thresholds")
    ap.add_argument("--fit-calibration", action="store_true",
                    help="fit cal_a/cal_b on these seasons and print them")
    ap.add_argument("--by-stat", action="store_true", help="also report per-stat")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set")
    conn = psycopg2.connect(dsn)

    all_preds: list[dict] = []
    for season in args.seasons.split(","):
        season = season.strip()
        print(f"Backtesting {season} ({args.mode}) ...", flush=True)
        n_before = len(all_preds)
        run_backtest(conn, season, args.mode, collect=all_preds)
        print(f"  {len(all_preds) - n_before:,} predictions")

    if not all_preds:
        sys.exit("No predictions generated.")

    print_report(all_preds, f"{args.mode.upper()} | seasons {args.seasons}")

    if args.by_stat:
        by_stat = defaultdict(list)
        for p in all_preds:
            by_stat[p["stat"]].append(p)
        for stat, group in sorted(by_stat.items()):
            print_report(group, f"stat = {stat}")

    if args.fit_calibration:
        a, b = fit_calibration(all_preds)
        print(f"\nFitted calibration on raw probs: cal_a={a:.4f}, cal_b={b:.4f}")
        print("Set these in ENGINE_PARAMS to bake in.")

    conn.close()


if __name__ == "__main__":
    main()
