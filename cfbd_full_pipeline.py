#!/usr/bin/env python3
"""
cfbd_full_pipeline.py (v2) — data builder for The Scouting Board static site.

What it does each run:
  1. Pulls the CURRENT season's FBS rosters -> defines the player set
     (QB / RB / WR / TE / ATH).
  2. Pulls season stats for every year back to --earliest-year and keeps every
     season in which a current-roster player accrued ANY stats (full college
     career, transfers included — season rows keep the school/conference where
     the stats actually happened).
  3. Zero-stat players are dropped entirely (bio-only rows are not emitted).
  4. Computes derived metrics (cmp%, Y/A, NCAA passer rating, Y/C, Y/R).
  5. Ranks the TOP 3 NFL comparables per season row against the reference
     library in data/comp_library.json (approximate lines — verify on
     Sports-Reference). Candidate #1 is the export default; the site lets you
     pick among the three.
  6. Writes:
       data/manifest.json                site index (conferences, years, counts)
       data/seasons_<conf-slug>.json     per-conference payloads (lazy-loaded)
       data/cfb_player_seasons.csv       full long-format CSV, spec sort order
     comp_library.json is read from data/ (source of truth for both site and
     pipeline).

Stable IDs / upserts:
  player_id = CFBD athlete id. Site-side edits are keyed on
  player_id|season_year in localStorage, so weekly regenerated data never
  clobbers your local tweaks.

Usage:
  export CFBD_API_KEY="..."      # free key at collegefootballdata.com/key
  python cfbd_full_pipeline.py                          # auto season, 6 yrs back
  python cfbd_full_pipeline.py --season 2026 --earliest-year 2021
  python cfbd_full_pipeline.py --with-games             # derive games_played
                                                        # (adds ~1 call/team/yr)

Free-tier note: a default run is roughly 150 core calls (135 rosters + team
list + per-conference stats per year). --with-games adds ~135 calls per season
pulled — enable it for the current season only if you're watching your quota
(--with-games-current).

Dependencies: pip install requests pandas
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date

import pandas as pd
import requests

API = os.environ.get("CFBD_API_BASE", "https://api.collegefootballdata.com")
SKILL_POS = {"QB", "RB", "WR", "TE", "ATH"}
CLASS_MAP = {1: "FR", 2: "SO", 3: "JR", 4: "SR", 5: "GR"}
POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}

CSV_COLUMNS = [
    "player_id", "full_name", "espn_id", "school", "conference", "position",
    "class_year", "height_in", "weight_lb", "hometown", "season_year",
    "games_played", "games_started",
    "pass_att", "pass_comp", "pass_cmp_pct", "pass_yds", "pass_td", "pass_int",
    "pass_yds_per_att", "passer_rating",
    "rush_att", "rush_yds", "rush_td", "rush_yds_per_att",
    "rec", "rec_yds", "rec_td", "rec_yds_per_rec",
    "nfl_comparable_name", "nfl_comparable_position", "nfl_comparable_college",
    "nfl_comparable_college_season_year", "nfl_comparable_college_stats_summary",
    "similarity_notes", "data_status",
]

STAT_MAP = {
    "passing":   {"COMPLETIONS": "pass_comp", "ATT": "pass_att", "YDS": "pass_yds",
                  "TD": "pass_td", "INT": "pass_int"},
    "rushing":   {"CAR": "rush_att", "YDS": "rush_yds", "TD": "rush_td"},
    "receiving": {"REC": "rec", "YDS": "rec_yds", "TD": "rec_td"},
}

COMP_FEATURES = {
    "QB": [("pass_yds", 1500), ("pass_td", 15), ("pass_int", 6),
           ("pass_cmp_pct", 8), ("rush_yds", 400), ("rush_td", 6)],
    "RB": [("rush_att", 90), ("rush_yds", 600), ("rush_td", 8),
           ("rec", 18), ("rec_yds", 200)],
    "WR": [("rec", 30), ("rec_yds", 450), ("rec_td", 7), ("rush_yds", 120)],
    "TE": [("rec", 18), ("rec_yds", 250), ("rec_td", 4)],
}


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #
def cfbd_get(path, api_key, params=None, retries=3):
    for attempt in range(retries):
        resp = requests.get(API + path, params=params,
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=45)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = 6 * (attempt + 1)
            print(f"  rate limited — sleeping {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"CFBD request failed after retries: {path}")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "unknown").lower()).strip("_")


# --------------------------------------------------------------------------- #
# Derived stats
# --------------------------------------------------------------------------- #
def derive(row):
    pa, pc = num(row.get("pass_att")), num(row.get("pass_comp"))
    py, pt, pi = num(row.get("pass_yds")), num(row.get("pass_td")), num(row.get("pass_int"))
    ra, ry = num(row.get("rush_att")), num(row.get("rush_yds"))
    rc, rcy = num(row.get("rec")), num(row.get("rec_yds"))
    row["pass_cmp_pct"] = round(100 * pc / pa, 1) if pa else ""
    row["pass_yds_per_att"] = round(py / pa, 2) if pa else ""
    row["passer_rating"] = (round((8.4 * py + 330 * pt - 200 * pi + 100 * pc) / pa, 1)
                            if pa else "")
    row["rush_yds_per_att"] = round(ry / ra, 2) if ra else ""
    row["rec_yds_per_rec"] = round(rcy / rc, 2) if rc else ""
    return row


# --------------------------------------------------------------------------- #
# Comps — top 3 candidates per row
# --------------------------------------------------------------------------- #
def load_library(data_dir):
    path = os.path.join(data_dir, "comp_library.json")
    if not os.path.exists(path):
        sys.exit(f"Missing {path} — the comp library ships with the repo.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["entries"]


def comp_summary(cand):
    s, parts = cand["s"], []
    if s.get("pass_yds"):
        parts.append(f"~{int(s['pass_yds']):,} pass yds, {int(s['pass_td'])} TD, "
                     f"{int(s['pass_int'])} INT, {s['pass_cmp_pct']}% cmp")
    if s.get("rush_yds") and cand["pos"] in ("QB", "RB"):
        bit = f"~{int(s['rush_yds'])} rush yds"
        if s.get("rush_td"):
            bit += f", {int(s['rush_td'])} rush TD"
        if s.get("rush_att"):
            bit += f" on {int(s['rush_att'])} att"
        parts.append(bit)
    if s.get("rec"):
        parts.append(f"~{int(s['rec'])} rec, {int(s['rec_yds'])} yds, "
                     f"{int(s.get('rec_td', 0))} TD")
    return "; ".join(parts) + " (approx.)"


def top_comps(row, library, k=3):
    feats = COMP_FEATURES.get(row["position"])
    if not feats:
        return []
    scored = []
    for cand in (c for c in library if c["pos"] == row["position"]):
        score, used = 0.0, 0
        for key, scale in feats:
            a, b = num(row.get(key)), num(cand["s"].get(key))
            if a == 0 and b == 0:
                continue
            score += ((a - b) / scale) ** 2
            used += 1
        if used:
            scored.append((score / used, cand))
    scored.sort(key=lambda x: x[0])
    out = []
    for score, cand in scored[:k]:
        out.append(dict(
            name=cand["name"], pos=cand["pos"], college=cand["college"],
            year=cand["year"], summary=comp_summary(cand),
            notes=(f"Auto-match (approx. library): {cand['profile']}. "
                   "Verify exact line on Sports-Reference."),
            score=round(score, 3),
        ))
    return out


# --------------------------------------------------------------------------- #
# Data pulls
# --------------------------------------------------------------------------- #
def pull_rosters(season, api_key):
    """Current-season rosters -> player set + bios + current conference."""
    print(f"[{season}] fetching FBS team list…")
    teams = cfbd_get("/teams/fbs", api_key, {"year": season})
    players = {}
    for i, t in enumerate(teams, 1):
        school, conf = t["school"], t.get("conference", "")
        try:
            roster = cfbd_get("/roster", api_key, {"team": school, "year": season})
        except Exception as exc:  # noqa: BLE001
            print(f"  roster FAILED {school}: {exc}")
            continue
        for p in roster:
            pos = (p.get("position") or "").upper()
            if pos not in SKILL_POS:
                continue
            pid = str(p.get("id"))
            first = p.get("firstName") or p.get("first_name") or ""
            last = p.get("lastName") or p.get("last_name") or ""
            players[pid] = dict(
                player_id=pid,
                full_name=f"{first} {last}".strip() or "Unknown",
                espn_id="",
                school=school,
                conference=conf,
                position=pos,          # ATH kept as ATH; comps treat as WR
                class_year=CLASS_MAP.get(p.get("year"), ""),
                height_in=p.get("height") or "",
                weight_lb=p.get("weight") or "",
                hometown=", ".join(x for x in (
                    p.get("homeCity") or p.get("home_city"),
                    p.get("homeState") or p.get("home_state")) if x),
            )
        if i % 20 == 0:
            print(f"  rosters {i}/{len(teams)}")
        time.sleep(0.12)
    print(f"[{season}] {len(players)} skill-position players on current rosters.")
    return teams, players


def pull_season_stats(year, api_key, teams, players):
    """One year of stats. Keeps only current-roster players with real stats.
    School/conference on the row = where the stats happened that year."""
    rows = {}
    confs = sorted({t.get("conference", "") for t in teams if t.get("conference")})
    for conf in confs:
        try:
            stats = cfbd_get("/stats/player/season", api_key,
                             {"year": year, "conference": conf})
        except Exception as exc:  # noqa: BLE001
            print(f"  [{year}] stats FAILED {conf}: {exc}")
            continue
        for s in stats:
            fmap = STAT_MAP.get(s.get("category"))
            if not fmap:
                continue
            field = fmap.get(str(s.get("statType", "")).upper())
            if not field:
                continue
            pid = str(s.get("playerId") or s.get("player_id") or "")
            bio = players.get(pid)
            if not bio:
                continue
            key = (pid, year)
            if key not in rows:
                rows[key] = dict(
                    player_id=pid,
                    full_name=bio["full_name"],
                    school=s.get("team") or bio["school"],
                    conference=s.get("conference") or conf,
                    position=bio["position"],
                    class_year=bio["class_year"],
                    season_year=year,
                    games_played="", games_started="",
                    data_status="CFBD_SYNC",
                )
            rows[key][field] = num(s.get("stat"))
        time.sleep(0.2)
    print(f"  [{year}] {len(rows)} player-season rows.")
    return rows


def derive_games(year, api_key, teams, rows):
    """Best-effort games_played from per-game box scores (1 call/team)."""
    seen = {}
    for i, t in enumerate(teams, 1):
        try:
            games = cfbd_get("/games/players", api_key,
                             {"year": year, "team": t["school"],
                              "seasonType": "regular"})
        except Exception as exc:  # noqa: BLE001
            print(f"  games FAILED {t['school']} {year}: {exc}")
            continue
        for g in games:
            gid = g.get("id")
            for side in g.get("teams", []):
                for cat in side.get("categories", []):
                    for typ in cat.get("types", []):
                        for ath in typ.get("athletes", []):
                            pid = str(ath.get("id"))
                            seen.setdefault(pid, set()).add(gid)
        if i % 25 == 0:
            print(f"  games {i}/{len(teams)} teams ({year})")
        time.sleep(0.12)
    for (pid, yr), row in rows.items():
        if yr == year and pid in seen:
            row["games_played"] = len(seen[pid])


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def write_outputs(data_dir, players, all_rows, library, season, years):
    os.makedirs(data_dir, exist_ok=True)

    # comps for every row
    for row in all_rows.values():
        derive(row)
        comp_pos = "WR" if row["position"] == "ATH" else row["position"]
        row["comp_candidates"] = top_comps({**row, "position": comp_pos}, library)

    # group rows by CURRENT roster conference (site loads one file at a time)
    by_conf = {}
    for (pid, yr), row in all_rows.items():
        conf = players[pid]["conference"] or "Unknown"
        by_conf.setdefault(conf, {"players": {}, "seasons": []})
        by_conf[conf]["players"][pid] = players[pid]
        by_conf[conf]["seasons"].append(row)

    manifest = {"generated": date.today().isoformat(),
                "season": season, "years": years, "conferences": []}
    for conf in sorted(by_conf):
        fname = f"seasons_{slug(conf)}.json"
        payload = by_conf[conf]
        payload["seasons"].sort(key=lambda r: (
            POS_ORDER.get(r["position"], 9), r["school"], r["full_name"],
            -r["season_year"]))
        with open(os.path.join(data_dir, fname), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        manifest["conferences"].append({
            "name": conf, "slug": slug(conf), "file": fname,
            "players": len(payload["players"]),
            "rows": len(payload["seasons"]),
        })
        print(f"  wrote {fname}: {len(payload['seasons'])} rows")
    with open(os.path.join(data_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)

    # long CSV — spec sort, chosen comp = candidate #1
    csv_rows = []
    for (pid, yr), row in all_rows.items():
        bio = players[pid]
        c = (row.get("comp_candidates") or [{}])[0]
        merged = {**bio, **{k: v for k, v in row.items() if k != "comp_candidates"}}
        merged.update(
            nfl_comparable_name=c.get("name", ""),
            nfl_comparable_position=c.get("pos", ""),
            nfl_comparable_college=c.get("college", ""),
            nfl_comparable_college_season_year=c.get("year", ""),
            nfl_comparable_college_stats_summary=c.get("summary", ""),
            similarity_notes=c.get("notes", ""),
        )
        csv_rows.append({col: merged.get(col, "") for col in CSV_COLUMNS})

    df = pd.DataFrame(csv_rows, columns=CSV_COLUMNS)
    df["__pos"] = df["position"].map(POS_ORDER).fillna(9)
    df = df.sort_values(
        by=["__pos", "conference", "school", "full_name", "season_year"],
        ascending=[True, True, True, True, False]).drop(columns="__pos")

    csv_path = os.path.join(data_dir, "cfb_player_seasons.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        f.write("# data_sources: CollegeFootballData.com API (live pull); ESPN CFB, "
                "Sports-Reference CFB, Pro-Football-Reference/Stathead "
                "(verification & comparables)\n")
        f.write(f"# last_updated: {date.today().isoformat()}\n")
        f.write("# scope: All FBS skill-position players (QB, RB, WR, TE, ATH) "
                "with stats accrued in their college careers\n")
        f.write("# note: comparable stat summaries are approximate; verify on "
                "Sports-Reference before relying on them\n")
        df.to_csv(f, index=False)
    print(f"  wrote {csv_path}: {len(df)} rows")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    today = date.today()
    default_season = today.year if today.month >= 6 else today.year - 1
    ap.add_argument("--season", type=int, default=default_season,
                    help="Current season whose rosters define the player set")
    ap.add_argument("--earliest-year", type=int, default=None,
                    help="How far back to pull stats (default: season - 5)")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--with-games", action="store_true",
                    help="Derive games_played for ALL pulled years (heavy)")
    ap.add_argument("--with-games-current", action="store_true",
                    help="Derive games_played for the current season only")
    args = ap.parse_args()

    api_key = os.environ.get("CFBD_API_KEY")
    if not api_key:
        sys.exit("Set CFBD_API_KEY (free key at collegefootballdata.com/key)")

    earliest = args.earliest_year or args.season - 5
    years = list(range(earliest, args.season + 1))
    library = load_library(args.data_dir)

    teams, players = pull_rosters(args.season, api_key)

    all_rows = {}
    if not players:
        print(f"[{args.season}] no rosters published yet -- falling back to {args.season - 1}")
        args.season -= 1
        teams, players = pull_rosters(args.season, api_key)
    for year in years:
        print(f"[{year}] pulling season stats…")
        all_rows.update(pull_season_stats(year, api_key, teams, players))
        if args.with_games or (args.with_games_current and year == args.season):
            print(f"[{year}] deriving games played (best effort)…")
            derive_games(year, api_key, teams, all_rows)

    # drop players who never accrued a stat (Q6: no zero-stat rows)
    active = {pid for (pid, _yr) in all_rows}
    players = {pid: b for pid, b in players.items() if pid in active}
    print(f"{len(players)} players with stats, {len(all_rows)} season rows total.")

    write_outputs(args.data_dir, players, all_rows, library, args.season, years)
    print("Done.")


if __name__ == "__main__":
    main()
