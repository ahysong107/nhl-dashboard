"""Compare Venom's pasted NHL goal list with our board (chat-only, never published).

Save the user's paste as data/venom/<date>.txt, one player per line. Lines are parsed loosely:
  "1 Cutter Gauthier;ANA;92.1;D"    (rank name;TEAM;score;flags)  or just  "Cutter Gauthier"
Optional section headers in [brackets] (e.g. [goals], [viper]) split lists.

Usage: python3 scripts/compare_venom.py YYYY-MM-DD
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def parse(path):
    sections, cur = {}, "goals"
    for raw in open(path):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = m.group(1).lower()
            continue
        parts = [x.strip() for x in line.split(";")]
        name = re.sub(r"^\d+[.)]?\s*", "", parts[0])
        sections.setdefault(cur, []).append({"name": name, "team": parts[1] if len(parts) > 1 else None,
                                             "score": parts[2] if len(parts) > 2 else None,
                                             "flags": parts[3] if len(parts) > 3 else ""})
    return sections


def main(date):
    snap = json.load(open(ROOT / "site" / "data" / f"{date}.json"))
    ours = {norm(p["name"]): p for p in snap["players"].values()}
    # last names for loose matches ("C. Gauthier")
    by_last = {}
    for p in snap["players"].values():
        by_last.setdefault(norm(p["name"].split(" ")[-1]), []).append(p)
    lists = parse(ROOT / "data" / "venom" / f"{date}.txt")
    for sec, items in lists.items():
        print(f"== Venom [{sec}] ({len(items)} names)")
        matched = []
        for i, v in enumerate(items, 1):
            p = ours.get(norm(v["name"]))
            if not p:
                c = by_last.get(norm(v["name"].split(" ")[-1]), [])
                p = c[0] if len(c) == 1 else None
            matched.append((i, v, p))
        top_ours = {p["id"] for p in snap["players"].values() if p["rank"] <= 25}
        cons = [(i, v, p) for i, v, p in matched[:25] if p and p["id"] in top_ours]
        print(f"  CONSENSUS (Venom top-25 and our top-25): {len(cons)}")
        for i, v, p in cons:
            print(f"    V#{i:<3} us #{p['rank']:<3} {p['name']:<24} {p['team']} {p['p_goal']:.0%} {p['flags']}")
        print("  Venom names we rank low (check lineup/injury first):")
        for i, v, p in matched[:25]:
            if p and p["id"] not in top_ours:
                print(f"    V#{i:<3} us #{p['rank']:<3} {p['name']:<24} {p['p_goal']:.0%} line {p['line']} {p['pp'] or ''}")
        miss = [v["name"] for _, v, p in matched if not p]
        if miss:
            print(f"  NOT ON OUR BOARD ({len(miss)}): {', '.join(miss)}  <- check data/raw/{date}/dfo_lines.json")
        vnames = {p["id"] for _, _, p in matched if p}
        print("  Our top-15 that Venom doesn't list in this section:")
        for p in sorted(snap["players"].values(), key=lambda x: x["rank"])[:15]:
            if p["id"] not in vnames:
                print(f"    us #{p['rank']:<3} {p['name']:<24} {p['team']} {p['p_goal']:.0%} {p['flags']}")


if __name__ == "__main__":
    main(sys.argv[1])
