"""Grade a stored Ice Edge snapshot against what actually happened (chat-only, never published).

Usage: python3 scripts/backtest.py YYYY-MM-DD
Reads site/data/<date>.json (or data/archive/<date>.json) and the box scores for that date
(fetches any that aren't cached yet).
"""
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BOX = ROOT / "data" / "box"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) IceEdge/1.0"}


def load_snapshot(date):
    for p in (ROOT / "site" / "data" / f"{date}.json", ROOT / "data" / "archive" / f"{date}.json"):
        if p.exists():
            return json.load(open(p))
    sys.exit(f"no snapshot for {date}")


def actual_goals(snap):
    goals, played = {}, set()
    for g in snap["games"]:
        f = BOX / f"{g['id']}.json"
        if not f.exists():
            r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{g['id']}/boxscore", headers=UA, timeout=30)
            b = r.json()
            if b.get("gameState") not in ("OFF", "FINAL"):
                print(f"  {g['away']}@{g['home']} not final yet ({b.get('gameState')}), skipped")
                continue
            json.dump(b, open(f, "w"))
        b = json.load(open(f))
        for side in ("homeTeam", "awayTeam"):
            for grp in ("forwards", "defense"):
                for p in b["playerByGameStats"][side].get(grp, []):
                    goals[p["playerId"]] = p.get("goals", 0)
                    played.add(p["playerId"])
    return goals, played


def main(date):
    snap = load_snapshot(date)
    goals, played = actual_goals(snap)
    rows = []
    for p in snap["players"].values():
        if p["id"] not in played:
            continue
        rows.append((p, goals.get(p["id"], 0)))
    scratched = [p["name"] for p in snap["players"].values() if p["id"] not in played
                 and any(p["game"] == g["id"] for g in snap["games"])]
    n = len(rows)
    if not n:
        sys.exit("nothing graded yet")
    scored = sum(1 for _, g in rows if g > 0)
    exp = sum(p["p_goal"] for p, _ in rows)
    brier = sum((p["p_goal"] - (g > 0)) ** 2 for p, g in rows) / n
    base = scored / n
    brier0 = sum((base - (g > 0)) ** 2 for _, g in rows) / n
    print(f"{date}: {n} skaters graded, {scored} scored at least once vs {exp:.1f} expected "
          f"(goals {sum(g for _, g in rows)} vs {sum(p['lam'] for p, _ in rows):.1f} exp)")
    print(f"  Brier {brier:.4f} vs flat-rate {brier0:.4f} ({'better' if brier < brier0 else 'worse'})")
    print("  calibration:")
    for lo, hi in ((0, .1), (.1, .2), (.2, .3), (.3, .4), (.4, 1)):
        b = [(p, g) for p, g in rows if lo <= p["p_goal"] < hi]
        if b:
            print(f"    {lo:.0%}-{hi:.0%}: {sum(1 for _, g in b if g)}/{len(b)} = "
                  f"{sum(1 for _, g in b if g) / len(b):.0%} (pred {sum(p['p_goal'] for p, _ in b) / len(b):.0%})")
    by_ali = sorted(rows, key=lambda r: r[0]["rank"])
    for k in (10, 25):
        top = by_ali[:k]
        print(f"  Ali top-{k}: {sum(1 for _, g in top if g)}/{len(top)} scored "
              f"(pred {sum(p['p_goal'] for p, _ in top):.1f})")
    for flag in ("VIPER", "DUE", "HOT", "OVERPERFORMING"):
        b = [(p, g) for p, g in rows if flag in p["flags"]]
        if b:
            print(f"  {flag}: {sum(1 for _, g in b if g)}/{len(b)} scored (pred {sum(p['p_goal'] for p, _ in b):.1f}) "
                  + ", ".join(f"{p['name']}{'*' if g else ''}" for p, g in b[:12]))
    print("  top-15 Ali Score:")
    for p, g in by_ali[:15]:
        print(f"    #{p['rank']:>2} {p['name']:<24} {p['p_goal']:.0%}  {'GOAL' + (' x' + str(g) if g > 1 else '') if g else '-'}")
    for c in snap.get("parlays", []):
        hits = []
        for leg in c["legs"]:
            g = goals.get(leg["id"])
            need = 2 if leg["market"].startswith("2+") else 1
            hits.append(None if leg["id"] not in played else g >= need)
        res = "VOID" if None in hits else ("CASHED" if all(hits) else "lost")
        print(f"  {c['name']} parlay ({c['p']:.1%}): {res} - " + ", ".join(
            f"{l['name']} {'Y' if h else ('-' if h is None else 'N')}" for l, h in zip(c["legs"], hits)))
    if scratched:
        print(f"  in our lineups but did not play ({len(scratched)}): {', '.join(scratched[:20])}")


if __name__ == "__main__":
    main(sys.argv[1])
