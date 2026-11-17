"""Fetch everything the Ice Edge goal model needs for one date.

Sources (all free, no keys):
  - NHL API (api-web.nhle.com): schedule, every team's season schedule, box scores
    for every completed regular-season game (cached per game), current rosters.
  - MoneyPuck season summaries: per-skater/goalie/team expected goals (xG), by situation,
    for the current and previous season.
  - DailyFaceoff: tonight's starting goalies (Confirmed / Likely / unconfirmed) and each
    playing team's current line combinations + power-play units + injured list.

Usage: python3 scripts/fetch_data.py YYYY-MM-DD
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
BOX = ROOT / "data" / "box"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) IceEdge/1.0"}

SEASON = "20262027"
MP_YEAR = 2026  # MoneyPuck labels a season by its starting year
TEAMS = ['ANA', 'BOS', 'BUF', 'CAR', 'CBJ', 'CGY', 'CHI', 'COL', 'DAL', 'DET', 'EDM', 'FLA',
         'LAK', 'MIN', 'MTL', 'NJD', 'NSH', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'SEA', 'SJS',
         'STL', 'TBL', 'TOR', 'UTA', 'VAN', 'VGK', 'WPG', 'WSH']

S = requests.Session()
S.headers.update(UA)


def get(url, tries=3, as_json=True):
    for i in range(tries):
        try:
            r = S.get(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                return r.json() if as_json else r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"failed: {url}")


def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))["props"]["pageProps"] if m else None


def main(date):
    RAW.mkdir(parents=True, exist_ok=True)
    BOX.mkdir(parents=True, exist_ok=True)
    day = RAW / date
    day.mkdir(exist_ok=True)

    # 1. Tonight's schedule
    sched = get(f"https://api-web.nhle.com/v1/schedule/{date}")
    games = []
    for d in sched.get("gameWeek", []):
        if d["date"] == date:
            games = [g for g in d["games"] if g.get("gameType") == 2]
    json.dump(games, open(day / "schedule.json", "w"))
    print(f"{date}: {len(games)} regular-season games")

    # 2. Season schedules -> completed game ids
    def club(t):
        return t, get(f"https://api-web.nhle.com/v1/club-schedule-season/{t}/{SEASON}")
    with ThreadPoolExecutor(8) as ex:
        clubs = dict(ex.map(club, TEAMS))
    json.dump(clubs, open(RAW / "club_schedules.json", "w"))
    done = {}
    for t, d in clubs.items():
        for g in d["games"]:
            if g["gameType"] == 2 and g["gameState"] in ("OFF", "FINAL") and g["gameDate"] < date:
                done[g["id"]] = g["gameDate"]
    need = [gid for gid in done if not (BOX / f"{gid}.json").exists()]
    print(f"completed games: {len(done)}, fetching {len(need)} new box scores")

    def box(gid):
        b = get(f"https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore")
        if b and b.get("gameState") in ("OFF", "FINAL"):
            json.dump(b, open(BOX / f"{gid}.json", "w"))
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(box, need))

    # 3. Rosters (current teams, full names for matching)
    def roster(t):
        return t, get(f"https://api-web.nhle.com/v1/roster/{t}/current")
    with ThreadPoolExecutor(8) as ex:
        rosters = dict(ex.map(roster, TEAMS))
    json.dump(rosters, open(day / "rosters.json", "w"))

    # 4. MoneyPuck season summaries (current + previous)
    for yr, tag in ((MP_YEAR, "cur"), (MP_YEAR - 1, "prev")):
        for kind in ("skaters", "goalies", "teams"):
            f = RAW / f"mp_{kind}_{tag}.csv"
            if tag == "prev" and f.exists():
                continue  # last season is final, download once
            txt = get(f"https://moneypuck.com/moneypuck/playerData/seasonSummary/{yr}/regular/{kind}.csv",
                      as_json=False)
            f.write_text(txt)
            if tag == "cur":
                (day / f"mp_{kind}_cur.csv").write_text(txt)  # frozen copy for backtests

    # 5. DailyFaceoff starting goalies + line combos for tonight's teams
    pp = next_data(get(f"https://www.dailyfaceoff.com/starting-goalies/{date}", as_json=False)) or {}
    dfo_goalies = pp.get("data", [])
    json.dump(dfo_goalies, open(day / "dfo_goalies.json", "w"))
    slugs = {}
    for g in dfo_goalies:
        slugs[g["homeTeamAbbreviation"]] = g["homeTeamSlug"]
        slugs[g["awayTeamAbbreviation"]] = g["awayTeamSlug"]
    print(f"DFO goalie cards: {len(dfo_goalies)}")

    def lines(item):
        abbr, slug = item
        try:
            p = next_data(get(f"https://www.dailyfaceoff.com/teams/{slug}/line-combinations", as_json=False))
            return abbr, (p or {}).get("combinations")
        except RuntimeError:
            return abbr, None
    with ThreadPoolExecutor(6) as ex:
        combos = dict(ex.map(lines, slugs.items()))
    json.dump(combos, open(day / "dfo_lines.json", "w"))
    print(f"DFO line combos: {sum(1 for v in combos.values() if v)}/{len(combos)} teams")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y-%m-%d"))
