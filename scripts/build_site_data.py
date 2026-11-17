"""Build tonight's Ice Edge goal board from fetched data.

Model (per skater in tonight's projected lineup):
  expected goals tonight  lam = ES part + PP part
    ES rate/60  = blended xG/60 (5v5 + other non-PP situations) x finishing talent
    PP rate/60  = blended 5v4 xG/60 x finishing talent
    finishing   = (G + K) / (xG + K) over current + prior season, K = 12, capped 0.8..1.35
    current season fades in: prior-season weight = max(0.2, 1 - cur GP / 40)
    form        = last-10 shots/60 vs season shots/60, shrunk 70%, capped 0.88..1.15
    ES TOI      = 60% last-5 + 40% season non-PP TOI per game
    PP TOI      = 50% season 5v4 TOI/gm + 50% DailyFaceoff unit default (PP1 2.8, PP2 1.2, none 0.15)
    matchup     = opponent xGA/60 vs league (shrunk 50%) x projected goalie GA-vs-xGA (shrunk) x home/away
    PP part also x opponent PK xGA/60 (shrunk) x opponent penalties-taken rate (shrunk)
  P(goal) = 1 - exp(-lam);  P(2+) = 1 - exp(-lam)(1 + lam)

Ali Score (same shape as Diamond Edge's HR score):
  Baseline x0.6  = slate percentiles of goal rate/60 (35), xG/60 (25), shots/60 (15), high-danger
                   shots/60 (10), projected TOI (15)
  Opportunity x0.4 = matchup lift scaled proportionally (70) + PP role (30)
  + DUE bonus (4 or 7) when season goals trail xG and the player is shooting but in a drought.

Usage: python3 scripts/build_site_data.py YYYY-MM-DD
"""
import csv
import json
import math
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
BOX = ROOT / "data" / "box"
SITE = ROOT / "site"
HIST = ROOT / "history"
ARCHIVE = ROOT / "data" / "archive"
RETENTION_DAYS = 7

K_FINISH = 12.0
PP_UNIT_TOI = {"PP1": 2.8, "PP2": 1.2, None: 0.15}
HOME_MULT = {True: 1.03, False: 0.97}
BOARD_N = 80


# ---------------------------------------------------------------- helpers
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


def toi_min(s):
    if not s:
        return 0.0
    m, sec = s.split(":")
    return int(m) + int(sec) / 60


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pctl_fn(values):
    vs = sorted(values)
    n = len(vs)

    def f(v):
        if n <= 1:
            return 50.0
        lo = sum(1 for x in vs if x < v)
        eq = sum(1 for x in vs if x == v)
        return 100.0 * (lo + 0.5 * (eq - 1)) / (n - 1)
    return f


def read_mp(path):
    out = defaultdict(dict)
    if not path.exists():
        return out
    for r in csv.DictReader(open(path)):
        key = r.get("playerId") or r.get("team")
        out[key][r["situation"]] = r
    return out


# ---------------------------------------------------------------- loaders
def load_boxes(before):
    """Per-player game logs, team game logs and goalie starts from cached box scores."""
    plogs = defaultdict(list)
    tlogs = defaultdict(list)
    gstarts = defaultdict(list)
    for f in BOX.glob("*.json"):
        b = json.load(open(f))
        d = b["gameDate"]
        if d >= before:
            continue
        h, a = b["homeTeam"], b["awayTeam"]
        for side, team, opp in (("homeTeam", h, a), ("awayTeam", a, h)):
            tlogs[team["abbrev"]].append({"d": d, "gid": b["id"], "opp": opp["abbrev"],
                                          "gf": team.get("score", 0), "ga": opp.get("score", 0),
                                          "home": side == "homeTeam"})
            st = b["playerByGameStats"][side]
            for grp in ("forwards", "defense"):
                for p in st.get(grp, []):
                    plogs[p["playerId"]].append({
                        "d": d, "gid": b["id"], "team": team["abbrev"], "opp": opp["abbrev"],
                        "home": side == "homeTeam", "g": p.get("goals", 0), "a": p.get("assists", 0),
                        "sog": p.get("sog", 0), "ppg": p.get("powerPlayGoals", 0),
                        "toi": round(toi_min(p.get("toi")), 2)})
            for g in st.get("goalies", []):
                if g.get("starter"):
                    gstarts[team["abbrev"]].append({"d": d, "id": g["playerId"], "name": g["name"]["default"]})
    for k in plogs:
        plogs[k].sort(key=lambda x: x["d"], reverse=True)
    for k in tlogs:
        tlogs[k].sort(key=lambda x: x["d"], reverse=True)
    for k in gstarts:
        gstarts[k].sort(key=lambda x: x["d"], reverse=True)
    return plogs, tlogs, gstarts


def roster_index(rosters):
    by_team = {}
    people = {}
    for t, r in rosters.items():
        idx = {"num": {}, "name": {}, "last": defaultdict(list)}
        for grp in ("forwards", "defensemen", "goalies"):
            for p in (r or {}).get(grp, []):
                full = f'{p["firstName"]["default"]} {p["lastName"]["default"]}'
                rec = {"id": p["id"], "name": full, "pos": p["positionCode"], "num": p.get("sweaterNumber"),
                       "team": t, "last": p["lastName"]["default"]}
                people[p["id"]] = rec
                idx["name"][norm(full)] = rec
                idx["last"][norm(p["lastName"]["default"])].append(rec)
                if p.get("sweaterNumber") is not None:
                    idx["num"][p["sweaterNumber"]] = rec
        by_team[t] = idx
    return by_team, people


def match_dfo(idx, dp):
    """Map a DailyFaceoff player to an NHL id: exact name, then jersey+last name, then unique last name."""
    rec = idx["name"].get(norm(dp["name"]))
    if rec:
        return rec
    last = norm(dp["name"].split(" ")[-1])
    r2 = idx["num"].get(dp.get("jerseyNumber"))
    if r2 and norm(r2["last"]).endswith(last[-4:]):
        return r2
    cands = idx["last"].get(last, [])
    if len(cands) == 1:
        return cands[0]
    return None


# ---------------------------------------------------------------- main build
def build(date):
    day = RAW / date
    games = json.load(open(day / "schedule.json"))
    rosters = json.load(open(day / "rosters.json"))
    dfo_g = json.load(open(day / "dfo_goalies.json"))
    dfo_l = json.load(open(day / "dfo_lines.json"))
    mp_sk = read_mp(RAW / "mp_skaters_cur.csv")
    mp_skp = read_mp(RAW / "mp_skaters_prev.csv")
    mp_go = read_mp(RAW / "mp_goalies_cur.csv")
    mp_gop = read_mp(RAW / "mp_goalies_prev.csv")
    mp_tm = read_mp(RAW / "mp_teams_cur.csv")
    plogs, tlogs, gstarts = load_boxes(date)
    ridx, people = roster_index(rosters)
    notes = []

    # ---- league team environment
    def team_rates(t):
        a = mp_tm.get(t, {}).get("all")
        pk = mp_tm.get(t, {}).get("4on5")
        if not a:
            return None
        it = fnum(a["iceTime"]) / 60 or 1
        pk_it = fnum(pk["iceTime"]) / 60 if pk else 0
        gp = fnum(a["games_played"]) or 1
        return {"gp": gp, "xgf60": fnum(a["xGoalsFor"]) / it * 60, "xga60": fnum(a["xGoalsAgainst"]) / it * 60,
                "gf_gp": fnum(a["goalsFor"]) / gp, "ga_gp": fnum(a["goalsAgainst"]) / gp,
                "pk_xga60": (fnum(pk["xGoalsAgainst"]) / pk_it * 60) if pk_it else None,
                "pk_min_gp": pk_it / gp, "pen_gp": fnum(a["penaltiesAgainst"]) / gp if a.get("penaltiesAgainst") else None}
    trates = {t: team_rates(t) for t in mp_tm}
    trates = {t: v for t, v in trates.items() if v}
    lg_xga60 = sum(v["xga60"] for v in trates.values()) / len(trates)
    lg_pk = [v["pk_xga60"] for v in trates.values() if v["pk_xga60"]]
    lg_pk_xga60 = sum(lg_pk) / len(lg_pk)
    lg_pkmin = sum(v["pk_min_gp"] for v in trates.values()) / len(trates)

    def def_mult(t):
        v = trates.get(t)
        if not v:
            return 1.0
        shrink = clamp(v["gp"] / 82, 0.25, 0.6)
        return 1 + shrink * (v["xga60"] / lg_xga60 - 1)

    def pk_mult(t):
        v = trates.get(t)
        if not v or not v["pk_xga60"]:
            return 1.0, 1.0
        shrink = clamp(v["gp"] / 82, 0.2, 0.5)
        q = 1 + shrink * (v["pk_xga60"] / lg_pk_xga60 - 1)
        vol = 1 + shrink * (v["pk_min_gp"] / lg_pkmin - 1)  # how often they're shorthanded
        return clamp(q, 0.8, 1.25), clamp(vol, 0.85, 1.2)

    # ---- goalie quality: GA vs xGA, current + prior, shrunk
    def goalie_info(gid, gname):
        cur = mp_go.get(str(gid), {}).get("all")
        prv = mp_gop.get(str(gid), {}).get("all")
        ga = xga = gp = 0.0
        if cur:
            ga += fnum(cur["goals"]); xga += fnum(cur["xGoals"]); gp += fnum(cur["games_played"])
        if prv:
            ga += 0.6 * fnum(prv["goals"]); xga += 0.6 * fnum(prv["xGoals"])
        K = 30.0
        ratio = (ga + K) / (xga + K) if xga else 1.0
        svp = None
        if cur:
            sog = fnum(cur["ongoal"])
            svp = round(1 - fnum(cur["goals"]) / sog, 3) if sog else None
        return {"id": gid, "name": gname, "mult": round(clamp(ratio, 0.82, 1.2), 3),
                "gsax": round(fnum(cur["xGoals"]) - fnum(cur["goals"]), 1) if cur else None,
                "svp": svp, "gp": int(gp)}

    # ---- goalie for tonight: DFO card, else last starter / rotation
    dfo_by_team = {}
    for g in dfo_g:
        for side in ("home", "away"):
            dfo_by_team[g[f"{side}TeamAbbreviation"]] = {
                "name": g[f"{side}GoalieName"], "status": g.get(f"{side}NewsStrengthName") or "Projected"}

    def tonight_goalie(team):
        cand = dfo_by_team.get(team)
        idx = ridx.get(team)
        if cand and idx:
            rec = idx["name"].get(norm(cand["name"]))
            if not rec:
                last = norm(cand["name"].split(" ")[-1])
                c = idx["last"].get(last, [])
                rec = c[0] if len(c) == 1 else None
            if rec:
                gi = goalie_info(rec["id"], rec["name"])
                gi["status"] = cand["status"]
                return gi
        st = gstarts.get(team, [])
        if st:
            gi = goalie_info(st[0]["id"], people.get(st[0]["id"], {}).get("name", st[0]["name"]))
            gi["status"] = "Projected"
            return gi
        return {"id": None, "name": "TBD", "mult": 1.0, "status": "Unknown", "gsax": None, "svp": None, "gp": 0}

    # ---- lineups from DailyFaceoff, fallback = last game's skaters
    def lineup(team):
        c = dfo_l.get(team)
        idx = ridx.get(team)
        out = {}
        if c and idx:
            status = {}
            for p in c["players"]:
                if p["groupIdentifier"] == "ir":
                    status[p["name"]] = p.get("injuryStatus")
            for p in c["players"]:
                grp = p["groupIdentifier"]
                rec = match_dfo(idx, p)
                if not rec:
                    continue
                pid = rec["id"]
                if grp[:1] in ("f", "d") and grp[1:].isdigit():
                    st = status.get(p["name"])
                    if st in ("ir", "out"):
                        continue
                    o = out.setdefault(pid, {"rec": rec, "line": None, "pp": None, "gtd": False})
                    o["line"] = ("L" if grp[0] == "f" else "D") + grp[1:]
                    o["gtd"] = o["gtd"] or bool(p.get("gameTimeDecision")) or st == "dtd"
                elif grp in ("pp1", "pp2"):
                    if pid in out or True:
                        out.setdefault(pid, {"rec": rec, "line": None, "pp": None, "gtd": False})
                        if out[pid]["pp"] is None or grp == "pp1":
                            out[pid]["pp"] = grp.upper()
            out = {k: v for k, v in out.items() if v["line"]}  # PP-only names not in an ES line are out
            if out:
                return out, c.get("updatedAt"), "dfo"
        # fallback: skaters from the team's most recent game
        tl = tlogs.get(team, [])
        if tl:
            gid = tl[0]["gid"]
            for pid, logs in plogs.items():
                if logs and logs[0]["gid"] == gid and logs[0]["team"] == team:
                    rec = people.get(pid) or {"id": pid, "name": str(pid), "pos": "?", "team": team}
                    out[pid] = {"rec": rec, "line": None, "pp": None, "gtd": False}
        return out, None, "last_game"

    # ---- player model
    def season_split(row_by_sit, w):
        """Return dict of weighted totals for non-PP and PP situations."""
        r = {"gp": 0, "es_toi": 0, "es_xg": 0, "pp_toi": 0, "pp_xg": 0, "g": 0, "xg": 0, "sog": 0,
             "hd": 0, "toi": 0}
        a = row_by_sit.get("all")
        if not a:
            return r
        pp = row_by_sit.get("5on4")
        tot_toi = fnum(a["icetime"]) / 60
        pp_toi = fnum(pp["icetime"]) / 60 if pp else 0
        pp_xg = fnum(pp["I_F_xGoals"]) if pp else 0
        sh = row_by_sit.get("4on5")
        sh_toi = fnum(sh["icetime"]) / 60 if sh else 0
        sh_xg = fnum(sh["I_F_xGoals"]) if sh else 0
        r["gp"] = w * fnum(a["games_played"])
        r["toi"] = w * tot_toi
        r["es_toi"] = w * (tot_toi - pp_toi - sh_toi)
        r["es_xg"] = w * (fnum(a["I_F_xGoals"]) - pp_xg - sh_xg)
        r["pp_toi"] = w * pp_toi
        r["pp_xg"] = w * pp_xg
        r["g"] = w * fnum(a["I_F_goals"])
        r["xg"] = w * fnum(a["I_F_xGoals"])
        r["sog"] = w * fnum(a["I_F_shotsOnGoal"])
        r["hd"] = w * fnum(a["I_F_highDangerShots"])
        return r

    league_es = {"F": [0, 0], "D": [0, 0]}  # xg, toi for priors
    for pid, sits in mp_sk.items():
        a = sits.get("all")
        if a:
            k = "D" if a["position"] == "D" else "F"
            league_es[k][0] += fnum(a["I_F_xGoals"]); league_es[k][1] += fnum(a["icetime"]) / 60
    prior_xg60 = {k: (v[0] / v[1] * 60 if v[1] else 0.5) for k, v in league_es.items()}

    def model_player(pid, rec, team, opp, home, info, goalie):
        logs = [l for l in plogs.get(pid, [])]
        cur_rows = mp_sk.get(str(pid), {})
        prv_rows = mp_skp.get(str(pid), {})
        cur = season_split(cur_rows, 1.0)
        gp_cur = cur["gp"]
        w_prev = max(0.2, 1 - gp_cur / 40)
        prv = season_split(prv_rows, w_prev)
        pos = rec.get("pos") or (cur_rows.get("all", {}).get("position")) or "F"
        pk = "D" if pos == "D" else "F"

        es_toi = cur["es_toi"] + prv["es_toi"]
        es_xg = cur["es_xg"] + prv["es_xg"]
        pp_toi_tot = cur["pp_toi"] + prv["pp_toi"]
        pp_xg = cur["pp_xg"] + prv["pp_xg"]
        # shrink ES rate toward positional prior with ~150 min of prior ice
        es_rate = (es_xg + prior_xg60[pk] / 60 * 150) / (es_toi + 150) * 60
        pp_prior = prior_xg60[pk] * 3.2
        pp_rate = (pp_xg + pp_prior / 60 * 40) / (pp_toi_tot + 40) * 60
        g_all = cur["g"] + prv["g"]
        xg_all = cur["xg"] + prv["xg"]
        finish = clamp((g_all + K_FINISH) / (xg_all + K_FINISH), 0.8, 1.35)

        # TOI projections
        recent = [l for l in logs if l["team"] == team][:5] or logs[:5]
        gp_tot = gp_cur + prv["gp"]
        season_es_gp = es_toi / gp_tot if gp_tot else (recent and sum(l["toi"] for l in recent) / len(recent) or 12)
        season_pp_gp = pp_toi_tot / gp_tot if gp_tot else 0
        recent_toi = sum(l["toi"] for l in recent) / len(recent) if recent else None
        if recent_toi is not None:
            recent_es = max(recent_toi - season_pp_gp, 0)
            es_proj = 0.6 * recent_es + 0.4 * season_es_gp
        else:
            es_proj = season_es_gp
        if not gp_tot and not recent:
            es_proj = 11.0 if pk == "F" else 15.0
        pp_proj = 0.5 * season_pp_gp + 0.5 * PP_UNIT_TOI[info["pp"]]

        # form: last-10 shots/60 vs season
        l10 = logs[:10]
        l10_toi = sum(l["toi"] for l in l10)
        season_sog60 = (cur["sog"] + prv["sog"]) / ((cur["toi"] + prv["toi"]) or 1) * 60
        form = 1.0
        if l10_toi > 60 and season_sog60 > 0:
            l10_sog60 = sum(l["sog"] for l in l10) / l10_toi * 60
            form = clamp(1 + 0.3 * (l10_sog60 / season_sog60 - 1), 0.88, 1.15)

        dm = def_mult(opp)
        pkq, pkvol = pk_mult(opp)
        gm = goalie["mult"]
        hm = HOME_MULT[home]
        lam_es = es_rate * finish * form * es_proj / 60 * dm * gm * hm
        lam_pp = pp_rate * finish * form * pp_proj * pkvol / 60 * pkq * gm * hm
        neutral = lam_es / (dm * gm * hm) + lam_pp / (pkq * pkvol * gm * hm)

        # season facts (current season, from MoneyPuck + box)
        cur_logs = [l for l in logs]
        gp_box = len(cur_logs)
        g_box = sum(l["g"] for l in cur_logs)
        drought = 0
        for l in cur_logs:
            if l["g"] > 0:
                break
            drought += 1
        sog_since = sum(l["sog"] for l in cur_logs[:drought])
        a_cur = cur_rows.get("all")
        cur_toi = cur["toi"] or 1
        p_prev = prv_rows.get("all")
        lift = dm * gm * hm * (0.85 + 0.15 * pkq * pkvol)

        return {
            "id": pid, "name": rec.get("name"), "team": team, "opp": opp, "home": home,
            "pos": pos, "line": info["line"], "pp": info["pp"], "gtd": info["gtd"],
            "_lam_es": lam_es, "_lam_pp": lam_pp, "_neutral": neutral,
            "proj_toi": round(es_proj + pp_proj, 1), "proj_pp_toi": round(pp_proj, 1),
            "es_xg60": round(es_rate, 3), "pp_xg60": round(pp_rate, 3), "finish": round(finish, 3),
            "form": round(form, 3), "def_mult": round(dm, 3), "pk_mult": round(pkq * pkvol, 3),
            "goalie_mult": gm, "lift": round(lift, 3),
            "season": {
                "gp": int(fnum(a_cur["games_played"])) if a_cur else gp_box,
                "g": int(fnum(a_cur["I_F_goals"])) if a_cur else g_box,
                "xg": round(fnum(a_cur["I_F_xGoals"]), 1) if a_cur else None,
                "sog": int(fnum(a_cur["I_F_shotsOnGoal"])) if a_cur else sum(l["sog"] for l in cur_logs),
                "ppg": sum(l["ppg"] for l in cur_logs),
                "a": sum(l["a"] for l in cur_logs),
                "toi_gp": round(cur["toi"] / gp_cur, 1) if gp_cur else None,
                "g60": round(cur["g"] / cur_toi * 60, 2) if a_cur else None,
                "xg60": round(cur["xg"] / cur_toi * 60, 2) if a_cur else None,
                "sog60": round(cur["sog"] / cur_toi * 60, 1) if a_cur else None,
                "hd60": round(cur["hd"] / cur_toi * 60, 2) if a_cur else None,
            },
            "prev": {"gp": int(fnum(p_prev["games_played"])), "g": int(fnum(p_prev["I_F_goals"])),
                     "xg": round(fnum(p_prev["I_F_xGoals"]), 1)} if p_prev else None,
            "drought": drought, "sog_since": sog_since,
            "l5_g": sum(l["g"] for l in logs[:5]), "l5_sog": sum(l["sog"] for l in logs[:5]),
            "l10_g": sum(l["g"] for l in l10), "l10_sog": sum(l["sog"] for l in l10),
            "log": [{"d": l["d"][5:], "opp": l["opp"], "h": l["home"], "g": l["g"], "a": l["a"],
                     "sog": l["sog"], "toi": round(l["toi"], 1)} for l in logs[:10]],
            # rate inputs for the Ali Score
            "_xg60": es_rate, "_sog60": season_sog60,
            "_hd60": (cur["hd"] + prv["hd"]) / ((cur["toi"] + prv["toi"]) or 1) * 60,
        }

    # ---- run over tonight's games
    out_games = []
    players = {}
    for g in sorted(games, key=lambda x: x["startTimeUTC"]):
        h, a = g["homeTeam"]["abbrev"], g["awayTeam"]["abbrev"]
        gh, ga = tonight_goalie(h), tonight_goalie(a)
        gcard = {"id": g["id"], "start": g["startTimeUTC"], "home": h, "away": a,
                 "venue": g.get("venue", {}).get("default"), "state": g.get("gameState"),
                 "goalie": {h: gh, a: ga}, "teams": {}, "players": {h: [], a: []}, "lineup_src": {}}
        for team, opp, home in ((h, a, True), (a, h, False)):
            lu, upd, src = lineup(team)
            gcard["lineup_src"][team] = {"src": src, "updated": upd}
            if src != "dfo":
                notes.append(f"{team}: no DailyFaceoff lines, using last game's skaters")
            opp_goalie = gcard["goalie"][opp]
            for pid, info in lu.items():
                p = model_player(pid, info["rec"], team, opp, home, info, opp_goalie)
                p["opp_goalie"] = opp_goalie["name"]
                p["game"] = g["id"]
                # missed team's recent games?
                tl = [x["gid"] for x in tlogs.get(team, [])[:3]]
                mine = {l["gid"] for l in plogs.get(pid, [])}
                p["missed_recent"] = sum(1 for x in tl if x not in mine) if tl else 0
                players[pid] = p
                gcard["players"][team].append(pid)
        out_games.append(gcard)

    # ---- calibration: scale so an average lineup in a neutral matchup scores the league's
    # actual skater goals per team-game (from every box score this season, empty-netters included)
    tot_g = sum(l["g"] for logs in plogs.values() for l in logs)
    team_games = sum(len(v) for v in tlogs.values())
    lg_goals = tot_g / team_games if team_games else 3.0
    team_neutral = [sum(players[x]["_neutral"] for x in g["players"][t]) for g in out_games for t in g["players"]]
    calib = lg_goals / (sum(team_neutral) / len(team_neutral)) if team_neutral else 1.0
    for p in players.values():
        lam_es, lam_pp = p.pop("_lam_es") * calib, p.pop("_lam_pp") * calib
        p.pop("_neutral")
        lam = lam_es + lam_pp
        p["lam"] = round(lam, 4)
        p["lam_pp"] = round(lam_pp, 4)
        p["p_goal"] = round(1 - math.exp(-lam), 4)
        p["p_2g"] = round(1 - math.exp(-lam) * (1 + lam), 4)
        p["_rate60"] = lam / max(p["proj_toi"], 1) * 60
    for g in out_games:
        for team in g["players"]:
            tr = trates.get(team) or {}
            g["teams"][team] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tr.items()}
            g["teams"][team]["exp_goals"] = round(sum(players[x]["lam"] for x in g["players"][team]), 2)
    notes.append(f"calibration x{calib:.3f} (league {lg_goals:.2f} skater goals per team-game)")

    # ---- Ali Score over the slate (percentile pool = every skater in tonight's lineups)
    pool = list(players.values())
    if pool:
        f_rate = pctl_fn([p["_rate60"] for p in pool])
        f_xg = pctl_fn([p["_xg60"] for p in pool])
        f_sog = pctl_fn([p["_sog60"] for p in pool])
        f_hd = pctl_fn([p["_hd60"] for p in pool])
        f_toi = pctl_fn([p["proj_toi"] for p in pool])
        for p in pool:
            base = (0.35 * f_rate(p["_rate60"]) + 0.25 * f_xg(p["_xg60"]) + 0.15 * f_sog(p["_sog60"])
                    + 0.10 * f_hd(p["_hd60"]) + 0.15 * f_toi(p["proj_toi"]))
            opp_s = clamp(50 + (p["lift"] - 1) * 300, 0, 100)
            pp_s = {"PP1": 100, "PP2": 45, None: 0}[p["pp"]]
            opp_total = 0.7 * opp_s + 0.3 * pp_s
            s = p["season"]
            gap = (s["xg"] or 0) - (s["g"] or 0)
            due = 0
            if gap >= 1.5 and p["drought"] >= 4 and p["sog_since"] >= 8:
                due = 7 if (gap >= 3 or p["drought"] >= 7) else 4
            flags = []
            if due:
                flags.append("DUE")
            if s["xg"] is not None and s["g"] - s["xg"] >= 3 and p["finish"] >= 1.2:
                flags.append("OVERPERFORMING")
            if p["l5_g"] >= 3:
                flags.append("HOT")
            p["base"] = round(base, 1)
            p["opp_score"] = round(opp_total, 1)
            p["due_bonus"] = due
            p["ali"] = round(min(100, 0.6 * base + 0.4 * opp_total + due), 1)
            p["_flags"] = flags
        # VIPER: elite baseline + clearly favorable matchup
        for p in pool:
            if p["base"] >= 85 and p["lift"] >= 1.06:
                p["_flags"].insert(0, "VIPER")
            p["flags"] = p.pop("_flags")
            for k in ("_rate60", "_xg60", "_sog60", "_hd60"):
                p.pop(k, None)

    board = sorted(pool, key=lambda p: (-p["ali"], -p["p_goal"]))
    for i, p in enumerate(board, 1):
        p["rank"] = i
    by_prob = sorted(pool, key=lambda p: -p["p_goal"])
    for i, p in enumerate(by_prob, 1):
        p["prob_rank"] = i

    # ---- rank movement vs the most recent of the last 3 days
    prev_rank = {}
    for back in range(1, 4):
        f = HIST / f"{(Date.fromisoformat(date) - timedelta(days=back)).isoformat()}.json"
        if f.exists():
            prev_rank = {int(k): v for k, v in json.load(open(f)).items()}
            break
    for p in board:
        pr = prev_rank.get(p["id"])
        p["mv"] = None if not prev_rank else ("new" if pr is None else pr - p["rank"])
    HIST.mkdir(exist_ok=True)
    json.dump({p["id"]: p["rank"] for p in board}, open(HIST / f"{date}.json", "w"))

    # ---- summary line per player
    for p in pool:
        s = p["season"]
        bits = [f'{s["g"]} G in {s["gp"]} GP']
        if s["xg"] is not None:
            bits.append(f'{s["xg"]} xG')
        if p["drought"] >= 5:
            bits.append(f'{p["drought"]}-game drought')
        elif p["l5_g"] >= 2:
            bits.append(f'{p["l5_g"]} G last 5')
        bits.append(f'vs {p["opp_goalie"]}')
        p["summary"] = " · ".join(bits)

    parlays = build_parlays(board)
    league = league_context(mp_sk, plogs, people, date)

    snap = {
        "date": date,
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "games": out_games,
        "players": {str(p["id"]): p for p in pool},
        "board": [p["id"] for p in board[:BOARD_N]],
        "parlays": parlays,
        "league": league,
        "notes": notes,
        "totals": {"exp_goals_slate": round(sum(p["lam"] for p in pool), 1), "n_skaters": len(pool)},
    }
    (SITE / "data").mkdir(parents=True, exist_ok=True)
    json.dump(snap, open(SITE / "data" / f"{date}.json", "w"), separators=(",", ":"))
    prune(date)
    return snap


def build_parlays(board):
    """System parlays: one leg per game, anytime-goal legs (and one 2+ goal leg on Bold)."""
    def pick(cands, n, used_games):
        legs = []
        for p in cands:
            if p["game"] in used_games or p["gtd"]:
                continue
            legs.append(p)
            used_games.add(p["game"])
            if len(legs) == n:
                break
        return legs

    def card(name, desc, legs, market=None):
        prob = 1.0
        out = []
        for p, m in legs:
            q = p["p_2g"] if m == "2+" else p["p_goal"]
            prob *= q
            out.append({"id": p["id"], "name": p["name"], "team": p["team"], "opp": p["opp"],
                        "market": "2+ goals" if m == "2+" else "Anytime goal", "p": round(q, 4)})
        return {"name": name, "desc": desc, "legs": out, "p": round(prob, 4),
                "fair_odds": american(prob)}

    by_p = sorted(board, key=lambda p: -p["p_goal"])
    cards = []
    used = set()
    safe = pick(by_p, 2, used)
    if len(safe) == 2:
        cards.append(card("Safe", "Two highest goal chances on the slate, different games.", [(p, "1+") for p in safe]))
    mid = [p for p in sorted(board, key=lambda p: -p["ali"]) if 0.25 <= p["p_goal"] <= 0.42]
    used = set()
    std = pick(mid, 3, used)
    if len(std) == 3:
        cards.append(card("Standard", "Three top Ali Score shooters in the 25-42% range, one per game.",
                          [(p, "1+") for p in std]))
    used = set()
    taken = {leg["id"] for c in cards for leg in c["legs"]}
    viper = [p for p in board if ("VIPER" in p["flags"] or "DUE" in p["flags"]) and p["id"] not in taken]
    legs = pick(sorted(viper, key=lambda p: -p["ali"]), 2, used)
    two = pick(sorted(board, key=lambda p: -p["p_2g"]), 1, used)
    if len(legs) == 2 and two:
        cards.append(card("Bold", "Two VIPER/DUE shooters plus the slate's best 2+ goal chance.",
                          [(p, "1+") for p in legs] + [(two[0], "2+")]))
    return cards


def american(p):
    if p <= 0 or p >= 1:
        return None
    return f"+{round(100 * (1 - p) / p)}" if p < 0.5 else f"-{round(100 * p / (1 - p))}"


def league_context(mp_sk, plogs, people, date):
    rows = []
    for pid, sits in mp_sk.items():
        a = sits.get("all")
        if not a:
            continue
        rows.append({"id": int(pid), "name": a["name"], "team": a["team"], "gp": int(fnum(a["games_played"])),
                     "g": int(fnum(a["I_F_goals"])), "xg": round(fnum(a["I_F_xGoals"]), 1),
                     "sog": int(fnum(a["I_F_shotsOnGoal"]))})
    goals = sorted(rows, key=lambda r: (-r["g"], -r["xg"]))[:10]
    xg = sorted(rows, key=lambda r: -r["xg"])[:10]
    unlucky = sorted([r for r in rows if r["gp"] >= 8], key=lambda r: -(r["xg"] - r["g"]))[:10]
    hot = []
    for pid, logs in plogs.items():
        l5 = logs[:5]
        if len(l5) == 5:
            g5 = sum(l["g"] for l in l5)
            if g5 >= 4:
                hot.append({"id": pid, "name": people.get(pid, {}).get("name", str(pid)),
                            "team": l5[0]["team"], "g5": g5, "sog5": sum(l["sog"] for l in l5)})
    hot.sort(key=lambda r: (-r["g5"], -r["sog5"]))
    for i, r in enumerate(sorted(rows, key=lambda r: -r["g"]), 1):
        r["lg_rank"] = i
    return {"goals": goals, "xg": xg, "unlucky": unlucky, "hot": hot[:10]}


def prune(date):
    """Keep RETENTION_DAYS of browsable snapshots; move older ones to data/archive/."""
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    cutoff = (Date.fromisoformat(date) - timedelta(days=RETENTION_DAYS - 1)).isoformat()
    pruned = []
    for f in sorted((SITE / "data").glob("*.json")):
        if f.stem < cutoff:
            shutil.move(str(f), ARCHIVE / f.name)
            pruned.append(f.stem)
    dates = sorted((f.stem for f in (SITE / "data").glob("*.json")), reverse=True)
    json.dump(dates, open(SITE / "dates.json", "w"))
    json.dump(pruned, open(ROOT / "data" / "pruned_dates.json", "w"))
    if pruned:
        print("pruned (moved to archive):", ", ".join(pruned))


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else Date.today().isoformat()
    snap = build(d)
    pl = snap["players"]
    print(f"{d}: {len(snap['games'])} games, {len(pl)} skaters, slate exp goals {snap['totals']['exp_goals_slate']}")
    for g in snap["games"]:
        t = g["teams"]
        print(f"  {g['away']} @ {g['home']}: exp {t[g['away']]['exp_goals']} - {t[g['home']]['exp_goals']} "
              f"(actual GF/gp {t[g['away']].get('gf_gp')} / {t[g['home']].get('gf_gp')}) "
              f"G: {g['goalie'][g['away']]['name']} ({g['goalie'][g['away']]['status']}) / "
              f"{g['goalie'][g['home']]['name']} ({g['goalie'][g['home']]['status']})")
    for pid in snap["board"][:15]:
        p = pl[str(pid)]
        print(f"  #{p['rank']:>2} {p['name']:<24} {p['team']} {p['line'] or '-':>3} {p['pp'] or '':>3} "
              f"P={p['p_goal']:.1%} ali={p['ali']} base={p['base']} opp={p['opp_score']} {p['flags']}")
    for n in snap["notes"]:
        print("  note:", n)
