#!/usr/bin/env python3
"""
============================================================
  BASKETBALL GURU - NBA Nightly PRA Board
============================================================

  WHAT IT DOES:
  Answers one question: who stuffs the stat sheet TONIGHT?

  For every player on the slate it projects Points + Rebounds + Assists
  (the DraftKings PRA prop) from the bottom up:
    - How many minutes will he play? (recent role, who is OUT tonight,
      back-to-back, blowout risk from the spread)
    - What does he produce per minute, and how much of an injured
      teammate's usage does he soak up?
    - How fast and how high-scoring does Vegas expect the game to be?
    - What does the opponent give up as a team?

  Output: a ranked board by projected PRA with the split (PTS / REB / AST /
  3PM), the chance of clearing a ladder of lines, and the BOOST -- how far
  tonight's projection sits above or below his last-10 average, which is
  roughly where the books hang the line. A line checker on every card turns
  any DK line into a More / Less percentage.

  SETUP:
    pip install requests
    Optional: set GITHUB_TOKEN to deploy the board to GitHub Pages

  RUNNING:
    python BasketballGuru.py                  # tonight's slate
    python BasketballGuru.py --date 2026-01-15 --backtest --no-deploy
                                              # rebuild a past slate and grade it
    python BasketballGuru.py --calibrate 2025-11-15 2026-04-10
                                              # model check across a date range
============================================================
"""

import os
import re
import sys
import json
import math
import time
import requests
import webbrowser

from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")   # handles the EDT/EST switch itself
except Exception:                            # pragma: no cover
    from datetime import timezone
    EASTERN = timezone(timedelta(hours=-5))


def to_eastern(iso_utc):
    """ESPN publishes tip-offs in UTC; a 10:30 PM ET game reads as 02:30 tomorrow otherwise."""
    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(EASTERN)
    except Exception:
        return None


def now_et():
    return datetime.now(EASTERN)


# ============================================================
#  CONFIG
# ============================================================

OUTPUT_FOLDER      = "reports"
# Every slate's calls are logged here, then graded against the box scores.
PREDICTIONS_FOLDER = "predictions"
# Two tiers: cache/season/ holds finished box scores (they never change) and is
# committed so Actions runs start warm; cache/daily/ holds what goes stale.
CACHE_FOLDER       = "cache"

# GitHub Actions has no browser; the workflow sets GITHUB_ACTIONS=true.
AUTO_OPEN_BROWSER  = os.environ.get("GITHUB_ACTIONS", "") != "true"
DELAY              = 0.05
BOX_WORKERS        = 6         # parallel ESPN summary pulls (ESPN has not throttled at 6)

# --- Slate --------------------------------------------------------------------
# DraftKings' main NBA slate is the 7:00 PM ET wave; the board has a toggle to
# hide earlier tips.
MAIN_SLATE_START_ET = 18.9
# Exhibition games are only used when there is NO regular-season slate that
# day -- the week before opening night is a free end-to-end dry run.
INCLUDE_PRESEASON   = True

TOP_SHOWN = 30                  # cards shown before "Show all"
MIN_PROJ_MINUTES = 12.0         # below this a player is listed only under "Show all"

GRADE_COLORS = {"A+": "#00e676", "A": "#69f0ae", "B+": "#b9f6ca",
                "B": "#fff176", "C": "#ffb74d", "D": "#ff5252", "OUT": "#ff4444"}

# Grade anchors are ABSOLUTE projected PRA, not slate-relative: 40 PRA is an A+
# on a 3-game Tuesday and on a 13-game Friday alike.
PRA_GRADES = [("A+", 42.0), ("A", 36.0), ("B+", 31.0), ("B", 26.0), ("C", 20.0)]

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER   = "christopher2smithtrade-sketch"
GITHUB_REPO   = "BasketballGuru"
GITHUB_BRANCH = "main"
PAGES_URL     = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/"
ACTIONS_URL   = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/actions/workflows/basketball.yml"

# -- ntfy push notifications (same app as H-Bomb, Gridiron and Hockey Guru) -----
NTFY_ENABLED        = True
NTFY_TOPIC          = "basketball-guru"
NTFY_NOTIFY_SUCCESS = True      # False = alert only when something breaks

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
UA   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}

# ESPN abbreviation -> (ESPN team id, full name, primary colour). ESPN's own
# abbreviations (GS, NY, NO, SA, UTAH, WSH) are the keys everywhere.
TEAMS = {
    "ATL": ("1", "Atlanta Hawks", "#c8102e"),          "BOS": ("2", "Boston Celtics", "#008348"),
    "BKN": ("17", "Brooklyn Nets", "#000000"),         "CHA": ("30", "Charlotte Hornets", "#008ca8"),
    "CHI": ("4", "Chicago Bulls", "#ce1141"),          "CLE": ("5", "Cleveland Cavaliers", "#860038"),
    "DAL": ("6", "Dallas Mavericks", "#0064b1"),       "DEN": ("7", "Denver Nuggets", "#0e2240"),
    "DET": ("8", "Detroit Pistons", "#1d428a"),        "GS":  ("9", "Golden State Warriors", "#fdb927"),
    "HOU": ("10", "Houston Rockets", "#ce0e2d"),       "IND": ("11", "Indiana Pacers", "#0c2340"),
    "LAC": ("12", "LA Clippers", "#12173f"),           "LAL": ("13", "Los Angeles Lakers", "#552583"),
    "MEM": ("29", "Memphis Grizzlies", "#5d76a9"),     "MIA": ("14", "Miami Heat", "#98002e"),
    "MIL": ("15", "Milwaukee Bucks", "#00471b"),       "MIN": ("16", "Minnesota Timberwolves", "#266092"),
    "NO":  ("3", "New Orleans Pelicans", "#0a2240"),   "NY":  ("18", "New York Knicks", "#f58426"),
    "OKC": ("25", "Oklahoma City Thunder", "#007ac1"), "ORL": ("19", "Orlando Magic", "#0150b5"),
    "PHI": ("20", "Philadelphia 76ers", "#1d428a"),    "PHX": ("21", "Phoenix Suns", "#e56020"),
    "POR": ("22", "Portland Trail Blazers", "#e03a3e"), "SAC": ("23", "Sacramento Kings", "#5a2d81"),
    "SA":  ("24", "San Antonio Spurs", "#c4ced4"),     "TOR": ("28", "Toronto Raptors", "#d91244"),
    "UTAH": ("26", "Utah Jazz", "#4e008e"),            "WSH": ("27", "Washington Wizards", "#e31837"),
}
ID_TO_ABBR = {v[0]: k for k, v in TEAMS.items()}


def display_color(hex_color):
    """Navy jerseys vanish on the dark board; lift anything too dark toward white."""
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    except Exception:
        return "#8b949e"
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum < 0.35:
        r, g, b = (int(c + (255 - c) * 0.5) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def notify(title, message, tags="", priority="default", click=None, run_button=True):
    """Push via ntfy.sh. A notification failure must never break a run."""
    if not NTFY_ENABLED:
        return
    try:
        headers = {"Title": title.encode("utf-8"), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if click:
            headers["Click"] = click
        actions = []
        if click:
            actions.append(f"view, Open board, {click}")
        if run_button:
            actions.append(f"view, Run workflow, {ACTIONS_URL}")
        if actions:
            headers["Actions"] = "; ".join(actions)
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print(f"  [!] Notify error: {e}")


def verify_live(timestamp, tries=4):
    """
    Confirm the live Pages site actually shows this run. A successful API push
    does not guarantee Pages rebuilt -- H-Bomb hit exactly this, and a stale
    board with no warning is worse than a failed run.
    """
    for attempt in range(1, tries + 1):
        try:
            time.sleep(20 if attempt > 1 else 8)
            r = requests.get(PAGES_URL, params={"cb": int(time.time())},
                             headers={"Cache-Control": "no-cache"}, timeout=20)
            if r.status_code == 200 and timestamp in r.text:
                print(f"  [OK] Live site verified (attempt {attempt})")
                return True
        except Exception as e:
            print(f"  verify attempt {attempt}/{tries}: {e}")
    print("  [!] LIVE SITE NOT UPDATED -- push succeeded but Pages is stale")
    return False


# ============================================================
#  SEASON / HELPERS
# ============================================================

def season_of(d):
    """ESPN labels a season by the year it ends: Oct 2026 - Jun 2027 is 2027."""
    return d.year + 1 if d.month >= 8 else d.year


def get_json(url, params=None, tries=4, timeout=25, headers=None):
    """GET with retries. One transient timeout must not drop a whole team from the board."""
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=headers or UA)
            if r.status_code == 429:
                time.sleep(5.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gave up after {tries} tries: {url}")


def fetch(url, params=None, label=""):
    try:
        data = get_json(url, params=params)
        time.sleep(DELAY)
        return data
    except Exception as e:
        print(f"  [!] {label or url}: {e}")
        return {}


def _norm_name(n):
    """Lowercase, strip accents/suffixes/punctuation, for cross-source name matching."""
    import unicodedata
    n = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode()
    n = n.lower()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    n = re.sub(r"[^a-z ]", "", n)
    return " ".join(n.split())


def _cache_path(key, permanent=False):
    return os.path.join(CACHE_FOLDER, "season" if permanent else "daily", f"{key}.json")


def cache_load(key, max_age_hours=None, permanent=False):
    """Cached JSON for key, or None if missing / unreadable / older than max_age_hours."""
    try:
        with open(_cache_path(key, permanent), "r", encoding="utf-8") as f:
            data = json.load(f)
        if max_age_hours is not None:
            age_h = (time.time() - data.get("_fetched_at", 0)) / 3600
            if age_h > max_age_hours:
                return None
        return data
    except Exception:
        return None


def cache_save(key, data, permanent=False):
    """Write data to the cache (stamped); failures are non-fatal."""
    try:
        path = _cache_path(key, permanent)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(data, dict):
            data["_fetched_at"] = time.time()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
    except Exception as e:
        print(f"  [!] Cache write {key}: {e}")


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def regress(rate, weight, prior, k):
    """Shrink an observed rate toward a prior; k is the sample size at which they split 50/50."""
    return (rate * weight + prior * k) / (weight + k) if weight + k > 0 else prior


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def american_to_prob(ml):
    ml = float(str(ml).replace("+", ""))
    return -ml / (-ml + 100) if ml < 0 else 100 / (ml + 100)


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


# ============================================================
#  DATA FETCHING
# ============================================================

def _parse_odds(odds, home, away):
    """ESPN pickcenter / scoreboard odds -> {spread (home), total, ml_home, ml_away}, DraftKings first."""
    if not odds:
        return None
    book = next((o for o in odds if "draftkings" in ((o.get("provider") or {}).get("name", "")).lower()), odds[0])
    out = {"total": None, "spread": None, "ml_home": None, "ml_away": None,
           "book": (book.get("provider") or {}).get("name", "")}
    try:
        if book.get("overUnder"):
            out["total"] = float(book["overUnder"])
    except Exception:
        pass
    # "details" is "ORL -5.5" -- the favourite and its number. The raw spread
    # field is not reliably home-relative across ESPN feeds, so parse the text.
    det = (book.get("details") or "").strip()
    m = re.match(r"^([A-Z]+)\s+(-?[\d.]+)$", det)
    if m:
        fav, pts = m.group(1), abs(float(m.group(2)))
        out["spread"] = -pts if fav == home else (pts if fav == away else None)
    elif det.upper() in ("EVEN", "PK", "PICK"):
        out["spread"] = 0.0
    for side, key in (("homeTeamOdds", "ml_home"), ("awayTeamOdds", "ml_away")):
        ml = (book.get(side) or {}).get("moneyLine")
        if ml not in (None, ""):
            try:
                out[key] = float(ml)
            except Exception:
                pass
    return out if (out["total"] or out["spread"] is not None) else None


def get_slate(slate_date):
    """Tonight's games from ESPN's scoreboard. Returns (games, preseason_flag)."""
    data = fetch(f"{ESPN}/scoreboard", params={"dates": slate_date.strftime("%Y%m%d")}, label="scoreboard")
    raw = data.get("events", [])
    by_type = {}
    for e in raw:
        by_type.setdefault((e.get("season") or {}).get("type", 2), []).append(e)
    picked = by_type.get(2, []) + by_type.get(3, []) + by_type.get(5, [])
    preseason = False
    if not picked and INCLUDE_PRESEASON:
        picked = by_type.get(1, [])
        preseason = bool(picked)

    games = []
    for e in picked:
        c = (e.get("competitions") or [{}])[0]
        comp = {x.get("homeAway"): x for x in c.get("competitors", [])}
        if "home" not in comp or "away" not in comp:
            continue
        home = ID_TO_ABBR.get(comp["home"]["team"]["id"], comp["home"]["team"].get("abbreviation"))
        away = ID_TO_ABBR.get(comp["away"]["team"]["id"], comp["away"]["team"].get("abbreviation"))
        if home not in TEAMS or away not in TEAMS:
            continue                     # All-Star / international exhibition opponents
        start = to_eastern(e.get("date", ""))
        hour = start.hour + start.minute / 60 if start else 19.0
        st = ((e.get("status") or {}).get("type") or {})
        games.append({
            "game_id": e["id"], "type": (e.get("season") or {}).get("type", 2),
            "home": home, "away": away,
            "home_name": TEAMS[home][1], "away_name": TEAMS[away][1],
            "start_et": start,
            "start_str": start.strftime("%I:%M %p").lstrip("0") if start else "TBD",
            "state": st.get("name", ""),
            "started": st.get("state", "pre") != "pre",
            "final": bool(st.get("completed")),
            "main_slate": hour >= MAIN_SLATE_START_ET,
            "odds_raw": _parse_odds(c.get("odds"), home, away),
            "home_score": comp["home"].get("score"), "away_score": comp["away"].get("score"),
        })
    games.sort(key=lambda x: (x["start_et"].timestamp() if x["start_et"] else 0, x["game_id"]))
    return games, preseason


# ── Box scores ───────────────────────────────────────────────
#
# Everything the model knows about players comes from box scores: minutes,
# the counting stats, who started, who sat and why. One compact file per
# finished game, committed, never re-pulled. A row is
#   [pid, team, starter, min, pts, reb, ast, fg3m, fg3a, fgm, fga, ftm, fta, oreb, tov, stl, blk]

BOX_COLS = ["pid", "team", "st", "min", "pts", "reb", "ast", "fg3m", "fg3a",
            "fgm", "fga", "ftm", "fta", "oreb", "tov", "stl", "blk"]
C = {k: i for i, k in enumerate(BOX_COLS)}


def _box_path(season, gid):
    return os.path.join(CACHE_FOLDER, "season", f"box_{season}", f"{gid}.json")


def _split(s):
    try:
        a, b = s.split("-")
        return int(a), int(b)
    except Exception:
        return 0, 0


def _int(s):
    try:
        return int(str(s).replace("+", ""))
    except Exception:
        return 0


def compact_box(summary, gid):
    """ESPN game summary -> compact box (or None if the game is not final)."""
    hdr = summary.get("header") or {}
    comp = (hdr.get("competitions") or [{}])[0]
    st = ((comp.get("status") or {}).get("type") or {})
    if not st.get("completed"):
        return None
    sides = {x.get("homeAway"): x for x in comp.get("competitors", [])}
    if "home" not in sides or "away" not in sides:
        return None
    home = ID_TO_ABBR.get(sides["home"]["team"]["id"], sides["home"]["team"].get("abbreviation"))
    away = ID_TO_ABBR.get(sides["away"]["team"]["id"], sides["away"]["team"].get("abbreviation"))
    start = to_eastern(comp.get("date", ""))
    rows, dnp, meta = [], [], {}
    for team_blk in (summary.get("boxscore") or {}).get("players", []):
        abbr = ID_TO_ABBR.get(str((team_blk.get("team") or {}).get("id")), (team_blk.get("team") or {}).get("abbreviation"))
        for grp in team_blk.get("statistics", [])[:1]:
            keys = grp.get("keys") or []
            idx = {k: i for i, k in enumerate(keys)}
            for a in grp.get("athletes", []):
                ath = a.get("athlete") or {}
                pid = str(ath.get("id", ""))
                if not pid:
                    continue
                meta[pid] = [ath.get("displayName", ""), ((ath.get("position") or {}).get("abbreviation") or "")]
                s = a.get("stats") or []
                if a.get("didNotPlay") or not s:
                    dnp.append([pid, abbr, (a.get("reason") or "").strip().upper()])
                    continue

                def v(k):
                    i = idx.get(k)
                    return s[i] if i is not None and i < len(s) else "0"
                fgm, fga = _split(v("fieldGoalsMade-fieldGoalsAttempted"))
                tpm, tpa = _split(v("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = _split(v("freeThrowsMade-freeThrowsAttempted"))
                mins = _int(v("minutes"))
                if mins <= 0 and fga == 0 and fta == 0 and _int(v("points")) == 0:
                    dnp.append([pid, abbr, (a.get("reason") or "DNP").strip().upper()])
                    continue
                rows.append([pid, abbr, 1 if a.get("starter") else 0, mins, _int(v("points")),
                             _int(v("rebounds")), _int(v("assists")), tpm, tpa, fgm, fga, ftm, fta,
                             _int(v("offensiveRebounds")), _int(v("turnovers")), _int(v("steals")),
                             _int(v("blocks"))])
    if not rows:
        return None
    # The header carries no period count; five players x 48 minutes is 240 a
    # side, and every overtime adds 25.
    team_min = {}
    for r in rows:
        team_min[r[1]] = team_min.get(r[1], 0) + r[3]
    periods = 4 + max(0, round((max(team_min.values()) - 240) / 25))
    return {
        "id": gid, "date": start.date().isoformat() if start else "",
        "type": (hdr.get("season") or {}).get("type", 2),
        "home": home, "away": away,
        "hs": _int(sides["home"].get("score")), "as": _int(sides["away"].get("score")),
        "periods": periods,
        "odds": _parse_odds(summary.get("pickcenter"), home, away),
        "p": rows, "dnp": dnp, "meta": meta,
    }


def get_box(gid, season):
    """Compact box for one game: from the committed cache if final, else ESPN."""
    path = _box_path(season, gid)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    data = fetch(f"{ESPN}/summary", params={"event": gid}, label=f"box {gid}")
    if not data:
        return None
    box = compact_box(data, gid)
    if box:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(box, f, separators=(",", ":"))
    return box


def _schedule_finished(season, preseason=False):
    """A past season never changes; nor does this season's preseason once October is well under way."""
    today = now_et().date()
    return season < season_of(today) or (preseason and today >= date(season - 1, 10, 25))


def season_schedule(season, preseason=False):
    """
    Every regular-season + postseason game of a season (or, with preseason,
    every exhibition): [(gid, et_date, completed)]. Built from the 30 team
    schedules. A finished schedule is cached for good; a live one is re-pulled
    at most every 6 hours.
    """
    key = f"{'preseason' if preseason else 'schedule'}_{season}"
    finished = _schedule_finished(season, preseason)
    cached = cache_load(key, permanent=finished) if finished else cache_load(key, 6)
    if cached:
        return cached["games"]
    games = {}

    def pull(team):
        out = []
        for st in ((1,) if preseason else (2, 3)):
            d = fetch(f"{ESPN}/teams/{TEAMS[team][0]}/schedule", params={"season": season, "seasontype": st},
                      label=f"schedule {team}")
            for e in d.get("events", []):
                c = (e.get("competitions") or [{}])[0]
                start = to_eastern(e.get("date", ""))
                done = bool(((c.get("status") or {}).get("type") or {}).get("completed"))
                out.append((e["id"], start.date().isoformat() if start else "", done))
        return out
    with ThreadPoolExecutor(max_workers=BOX_WORKERS) as ex:
        for lst in ex.map(pull, TEAMS):
            for gid, d, done in lst:
                games[gid] = (gid, d, done or games.get(gid, (0, 0, False))[2])
    out = sorted(games.values(), key=lambda x: (x[1], x[0]))
    cache_save(key, {"games": out}, permanent=finished)
    return out


def load_boxes(season, before, verbose=True, preseason=False):
    """All final box scores of `season` (or its preseason) dated before `before` (a date), oldest first."""
    sched = season_schedule(season, preseason)
    # A finished season only has the games that were actually played; the
    # current one trusts the date (the cached schedule can lag a few hours).
    finished = _schedule_finished(season, preseason)
    want = [(gid, d) for gid, d, done in sched if d and d < before.isoformat() and (done or not finished)]
    # A finished season's ids that ESPN has no summary for (NBA Cup
    # placeholders) are remembered, or every run would ask for them again.
    dead_path = os.path.join(CACHE_FOLDER, "season", f"box_{season}", "_missing.json")
    dead = set(_read_json(dead_path) or []) if finished else set()
    have, missing = [], []
    for gid, d in want:
        path = _box_path(season, gid)
        if os.path.exists(path):
            have.append(gid)
        elif gid not in dead:
            missing.append(gid)
    if missing and verbose:
        print(f"  Pulling {len(missing)} {'preseason ' if preseason else ''}box scores for {season - 1}-{str(season)[2:]} "
              f"({len(have)} cached)...")
    boxes = []
    if missing:
        done = 0
        with ThreadPoolExecutor(max_workers=BOX_WORKERS) as ex:
            for gid, b in zip(missing, ex.map(lambda g: get_box(g, season), missing)):
                done += 1
                if b is None and finished:
                    dead.add(gid)
                if verbose and done % 100 == 0:
                    print(f"    {done}/{len(missing)}")
        if finished and dead:
            os.makedirs(os.path.dirname(dead_path), exist_ok=True)
            with open(dead_path, "w", encoding="utf-8") as f:
                json.dump(sorted(dead), f)
    for gid, d in want:
        try:
            with open(_box_path(season, gid), "r", encoding="utf-8") as f:
                boxes.append(json.load(f))
        except Exception:
            pass
    boxes.sort(key=lambda b: (b["date"], b["id"]))
    return boxes


# ── Injuries / rosters ───────────────────────────────────────

# ESPN's NBA feed only says "Out" or "Day-To-Day"; the news blurb is what
# separates a true game-time decision from a player already ruled out.
RULED_OUT_RE = re.compile(r"(won't play|will not play|won't suit up|will not suit up|ruled out|"
                          r"is out for|will miss|won't be available|will not be available|"
                          r"sit out|not playing|inactive for|won't return)", re.I)
PLAYING_RE   = re.compile(r"(will play|is available|will be available|expected to play|"
                          r"cleared to play|will return|will start|in the starting lineup|"
                          r"upgraded to available)", re.I)


def get_injuries(slate_date):
    """ESPN's NBA injury feed -> {pid: {status, detail, comment, out}}."""
    data = fetch(f"{ESPN}/injuries", label="injuries")
    out = {}
    cutoff = datetime.combine(slate_date - timedelta(days=1), datetime.min.time()).replace(tzinfo=EASTERN)
    for team in data.get("injuries", []):
        for i in team.get("injuries", []):
            ath = i.get("athlete") or {}
            pid = str(ath.get("id") or "")
            link = next((l.get("href", "") for l in ath.get("links", []) if "/id/" in l.get("href", "")), "")
            if not pid and link:
                m = re.search(r"/id/(\d+)", link)
                pid = m.group(1) if m else ""
            if not pid:
                continue
            det = i.get("details") or {}
            detail = " ".join(x for x in (det.get("side", ""), det.get("type", ""), det.get("detail", ""))
                              if x and x != "Not Specified")
            status = i.get("status", "")
            comment = i.get("shortComment", "") or ""
            when = to_eastern(i.get("date", "") or "")
            fresh = bool(when and when >= cutoff)
            ruled_out = status.lower() == "out"
            note = ""
            if not ruled_out and fresh and RULED_OUT_RE.search(comment) and not PLAYING_RE.search(comment):
                ruled_out, note = True, "ruled out in latest report"
            elif not ruled_out and fresh and PLAYING_RE.search(comment):
                note = "expected to play"
            out[pid] = {"status": status, "detail": detail, "comment": comment[:240],
                        "out": ruled_out, "note": note, "return": det.get("returnDate", ""),
                        "name": ath.get("displayName", "")}
    return out


def get_rosters(teams):
    """Current rosters: {team: [{pid, name, pos, num, headshot}]}."""
    def pull(t):
        d = fetch(f"{ESPN}/teams/{TEAMS[t][0]}/roster", label=f"roster {t}")
        out = []
        for a in d.get("athletes", []):
            out.append({"pid": str(a["id"]), "name": a.get("displayName", ""),
                        "pos": ((a.get("position") or {}).get("abbreviation") or ""),
                        "num": a.get("jersey", ""), "team": t,
                        "headshot": ((a.get("headshot") or {}).get("href") or "")})
        return t, out
    cached = cache_load("rosters", 3) or {}
    want = [t for t in teams if t not in cached]
    if want:
        with ThreadPoolExecutor(max_workers=BOX_WORKERS) as ex:
            for t, lst in ex.map(pull, want):
                if lst:
                    cached[t] = lst
        cache_save("rosters", cached)
    return {t: cached.get(t, []) for t in teams}


# ── DraftKings player props ──────────────────────────────────
#
# ESPN's core API carries DraftKings' player props for free -- the same
# partnership that puts DK's spread and total on the scoreboard. Only the
# main PRA line ("Total Points, Rebounds, and Assists") is used. ESPN drops
# props some time after a game (of 2025-26 only 91 games kept theirs), so
# every line seen is archived in the committed tier.

CORE          = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
DK_PROVIDER   = "100"           # DraftKings in ESPN's odds feed
PRA_PROP_TYPE = "90"            # "Total Points, Rebounds, and Assists"


def fetch_pra_lines(gid):
    """DraftKings' main PRA line for every player in one game: {pid: [line, opening line]}; None on failure."""
    for attempt in range(3):
        try:
            r = requests.get(f"{CORE}/events/{gid}/competitions/{gid}/odds/{DK_PROVIDER}/propBets",
                             params={"lang": "en", "region": "us", "limit": 1000}, headers=UA, timeout=25)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            out = {}
            for it in r.json().get("items", []):
                if (it.get("type") or {}).get("id") != PRA_PROP_TYPE:
                    continue
                m = re.search(r"/athletes/(\d+)", (it.get("athlete") or {}).get("$ref", ""))
                cur = ((it.get("current") or {}).get("target") or {}).get("value")
                opn = ((it.get("open") or {}).get("target") or {}).get("value")
                if m and cur is not None:
                    out[m.group(1)] = [float(cur), float(opn) if opn is not None else None]
            return out
        except Exception as e:
            if attempt == 2:
                print(f"  [!] DK props {gid}: {e}")
            time.sleep(1.5 * (attempt + 1))
    return None


def get_dk_lines(games, season, live=True):
    """
    {game_id: {pid: [line, open]}} from the archive, refreshed from ESPN for
    every game that has not tipped yet. Once a game starts its line stops
    updating, so the archive keeps the last line before tip-off -- the one
    the board was judged on.
    """
    arch = cache_load(f"props_{season}", permanent=True) or {}
    lines = arch.get("lines", {})
    todo = [g["game_id"] for g in games if live and not g.get("started")]
    if todo:
        with ThreadPoolExecutor(max_workers=BOX_WORKERS) as ex:
            got = list(ex.map(fetch_pra_lines, todo))
        changed = False
        for gid, res in zip(todo, got):
            if res:
                lines[gid] = res
                changed = True
        if changed:
            cache_save(f"props_{season}", {"lines": lines}, permanent=True)
    return {g["game_id"]: lines.get(g["game_id"], {}) for g in games}


# ============================================================
#  PLAYER / TEAM DATABASE
# ============================================================

STATS = ("pts", "reb", "ast", "fg3m")


def pos_group(pos):
    p = (pos or "").upper()
    if p.startswith("C"):
        return "C"
    if p.startswith(("PG", "SG", "G")):
        return "G"
    return "F"


def _reg_scale(periods):
    """Per-game numbers are put on a 48-minute footing so a double-overtime night does not inflate a rate."""
    return 48.0 / (48.0 + 5.0 * max(0, (periods or 4) - 4))


class DB:
    """
    Every box score of two seasons, indexed by player and by team. All queries
    take a cut-off date and see only games BEFORE it, so a backtest of
    January 15 knows exactly what was knowable on the morning of January 15.
    """

    def __init__(self, boxes):
        from bisect import bisect_left
        self._bisect = bisect_left
        self.plog, self.tlog, self.meta = {}, {}, {}
        for b in sorted(boxes, key=lambda x: (x["date"], x["id"])):
            for pid, m in b.get("meta", {}).items():
                self.meta[pid] = m
        for b in sorted(boxes, key=lambda x: (x["date"], x["id"])):
            self._add(b)
        self.pdates = {pid: [g["d"] for g in lst] for pid, lst in self.plog.items()}
        self.tdates = {t: [g["d"] for g in lst] for t, lst in self.tlog.items()}

    def _add(self, b):
        season = season_of(date.fromisoformat(b["date"]))
        odds = b.get("odds") or {}
        sp_home, total = odds.get("spread"), odds.get("total")
        by_team = {}
        for r in b["p"]:
            by_team.setdefault(r[C["team"]], []).append(r)
        sides = {b["home"]: (b["away"], True, b["hs"], b["as"]), b["away"]: (b["home"], False, b["as"], b["hs"])}
        for t, (opp, home, pf, pa) in sides.items():
            rows, orows = by_team.get(t, []), by_team.get(opp, [])
            allowed = {}
            for r in orows:
                g = pos_group((self.meta.get(r[0]) or ["", ""])[1])
                a = allowed.setdefault(g, [0, 0, 0, 0, 0])
                a[0] += r[C["min"]]
                for j, k in enumerate(STATS, 1):
                    a[j] += r[C[k]]
            sp = None if sp_home is None else (sp_home if home else -sp_home)
            self.tlog.setdefault(t, []).append({
                "d": b["date"], "s": season, "type": b["type"], "gid": b["id"], "opp": opp, "home": home,
                "pts": pf, "opp_pts": pa, "periods": b["periods"], "spread": sp, "total": total,
                **{k: sum(r[C[k]] for r in rows) for k in ("reb", "ast", "fg3m")},
                **{f"opp_{k}": sum(r[C[k]] for r in orows) for k in ("reb", "ast", "fg3m")},
                "played": {r[0]: r[C["min"]] for r in rows},
                "out": {x[0]: x[2] for x in b["dnp"] if x[1] == t and "COACH" not in x[2]},
                "allowed": allowed,
            })
            for r in rows:
                self.plog.setdefault(r[0], []).append({
                    "d": b["date"], "s": season, "type": b["type"], "gid": b["id"], "team": t, "opp": opp,
                    "home": home, "played": True, "st": r[C["st"]], "min": r[C["min"]],
                    **{k: r[C[k]] for k in ("pts", "reb", "ast", "fg3m", "fg3a", "fga", "fta", "tov")},
                    "periods": b["periods"]})
            for pid, tm, reason in b["dnp"]:
                # A coach's-decision DNP is a real zero for minutes (the player
                # was healthy and did not get in); injury DNPs say nothing.
                if tm == t and "COACH" in reason:
                    self.plog.setdefault(pid, []).append({
                        "d": b["date"], "s": season, "type": b["type"], "gid": b["id"], "team": t,
                        "opp": opp, "home": home, "played": False, "st": 0, "min": 0, "periods": b["periods"]})

    def games(self, pid, before):
        """A player's games strictly before `before` (iso date), most recent first."""
        lst = self.plog.get(pid)
        if not lst:
            return []
        return lst[:self._bisect(self.pdates[pid], before)][::-1]

    def team_games(self, team, before):
        lst = self.tlog.get(team)
        if not lst:
            return []
        return lst[:self._bisect(self.tdates[team], before)][::-1]

    def name(self, pid):
        return (self.meta.get(pid) or ["?", ""])[0]

    def pos(self, pid):
        return (self.meta.get(pid) or ["", ""])[1]


# ============================================================
#  PROJECTION ENGINE
# ============================================================

# League per-48 team numbers and per-minute player rates by position group,
# used until the database has enough games to say otherwise (2025-26 levels).
LG_DEFAULT = {
    "pts": 115.0, "reb": 44.0, "ast": 26.8, "fg3m": 13.6, "total": 230.0,
    "pm": {"G": {"pts": 0.495, "reb": 0.150, "ast": 0.150, "fg3m": 0.072},
           "F": {"pts": 0.455, "reb": 0.205, "ast": 0.090, "fg3m": 0.055},
           "C": {"pts": 0.470, "reb": 0.330, "ast": 0.085, "fg3m": 0.022}},
}
# Minutes of evidence at which a player's own per-minute rate and the
# position average split 50/50. Rebounding and passing are role skills that
# show quickly; three-point makes need far more volume to separate from luck.
RATE_K = {"pts": 10.0, "reb": 10.0, "ast": 10.0, "fg3m": 30.0}
# Per-minute rates: this season's games decay by RATE_DECAY per game back;
# last season counts PRI_W as much and fades out over PRI_FADE_GAMES games.
RATE_DECAY, PRI_W, PRI_FADE_GAMES = 0.98, 0.2, 50
# Base minutes: MIN_RECENT_W on the last few games (decay MIN_RECENT_DECAY),
# the rest on the season with this team. Roles change fast in the NBA.
MIN_RECENT_W, MIN_RECENT_DECAY = 0.5, 0.7

TEAM_MINUTES = 241.5            # 240 + the overtime a typical night adds
MAX_MINUTES  = 42.0             # nobody is projected above this
# Filling a short-handed team: weight = (base + FLOOR)^ALPHA x (MAX - base)^BETA,
# times extra weight at the missing players' position. Trimming an overfull
# one takes from the bench first: base x (MAX - base)^TRIM_BETA.
FILL_FLOOR, FILL_ALPHA, FILL_BETA, FILL_POS = 4.0, 0.5, 1.5, 0.5
TRIM_BETA    = 3.0
HOME_PTS     = 1.4              # home edge on the scoreboard when there is no line
MODEL_MARGIN = 1.0              # share of the model's own margin kept when there is no line
# How hard each stat is pulled to the team total. 1.0 = the players' shares
# must add up to exactly what the team is expected to produce tonight.
NORM_GAMMA   = {"pts": 1.0, "reb": 1.0, "ast": 1.0, "fg3m": 1.0}
NORM_ZETA_UP, NORM_ZETA_DOWN = 0.3, -0.6
# Starters lose minutes in lopsided games: the per-point cut above BLOWOUT_FROM.
BLOWOUT_FROM = 6.0
BLOWOUT_K    = 0.0055
B2B_MIN      = 0.985           # minutes factor for starters on the 2nd night of a back-to-back
# Spread of outcomes around the projection: Var = a * mu^c, per stat, fitted
# on backtest residuals (so it includes the minutes uncertainty, not just
# shooting luck).
VAR_MODEL = {"pts": (6.13, 0.739), "reb": (1.84, 0.836), "ast": (1.40, 0.909), "fg3m": (1.31, 0.852),
             "pra": (10.84, 0.591)}
BOOST_VAR_K = 0.5               # extra PRA variance per squared point of boost
# Against a real DraftKings line the odds lean on the line, not the model:
# on 1,179 real 2025-26 PRA lines (92% playoffs) DK missed by 6.20 a player
# and the model by 6.45, and the model's 3+ point disagreements went 49%.
# The model's share of the say, fitted there; re-fit once the regular season
# has a few weeks of archived lines.
DK_MODEL_W  = 0.1
# Players with no NBA games are projected from this season's preseason.
PRE_MIN_F  = 0.7                # preseason minutes -> regular-season minutes (rookies play more in October)
PRE_RATE_K = 300.0              # preseason minutes at which his rate and the position average split 50/50
PRE_PRIOR_F = 0.8               # the position average he is shrunk toward, scaled (newcomers start below it)


def grade_for(score, table):
    for g, cut in table:
        if score >= cut:
            return g
    return "D"


def league_baselines(db, d, season):
    """League per-48 team numbers and per-minute rates by position, from both seasons before d."""
    lg = {k: v for k, v in LG_DEFAULT.items() if k != "pm"}
    lg["pm"] = {g: dict(v) for g, v in LG_DEFAULT["pm"].items()}
    tot = {k: 0.0 for k in ("n", "pts", "reb", "ast", "fg3m", "total_n", "total")}
    grp = {}
    for t, lst in db.tlog.items():
        for g in db.team_games(t, d):
            if g["s"] not in (season, season - 1) or g["type"] not in (2, 3):
                continue
            sc = _reg_scale(g["periods"])
            tot["n"] += 1
            tot["pts"] += g["pts"] * sc
            for k in ("reb", "ast", "fg3m"):
                tot[k] += g[k] * sc
            if g.get("total"):
                tot["total_n"] += 1
                tot["total"] += g["total"]
            for pg, a in g["allowed"].items():
                x = grp.setdefault(pg, [0.0] * 5)
                for i in range(5):
                    x[i] += a[i]
    if tot["n"] >= 100:
        for k in ("pts", "reb", "ast", "fg3m"):
            lg[k] = tot[k] / tot["n"]
    if tot["total_n"] >= 50:
        lg["total"] = tot["total"] / tot["total_n"]
    for pg, x in grp.items():
        if x[0] > 5000:
            lg["pm"][pg] = {k: x[i] / x[0] for i, k in enumerate(STATS, 1)}
    return lg


def team_profile(db, team, d, season, lg):
    """
    A team's per-48 production and what it allows, this season blended into
    last as games accumulate, plus rest. There is no defense-vs-position
    factor: in the 2025-26 replay it added nothing (taking it out entirely
    was marginally better) -- the opponent counts through the team totals.
    """
    tg = [g for g in db.team_games(team, d) if g["type"] in (2, 3) and g["s"] in (season, season - 1)]
    cur = [g for g in tg if g["s"] == season]
    pri = [g for g in tg if g["s"] == season - 1]
    wp = 0.5 * max(0.1, 1 - len(cur) / 30)
    ws = [(g, 0.97 ** i) for i, g in enumerate(cur)] + [(g, wp * 0.99 ** i) for i, g in enumerate(pri)]
    W = sum(w for _, w in ws)
    prof = {"gp_cur": len(cur), "games": len(tg)}

    def per48(key, fallback):
        if W <= 0:
            return fallback
        return sum(g[key] * _reg_scale(g["periods"]) * w for g, w in ws) / W
    prof["pts"], prof["opp_pts"] = per48("pts", lg["pts"]), per48("opp_pts", lg["pts"])
    for k in ("reb", "ast", "fg3m"):
        prof[k], prof[f"opp_{k}"] = per48(k, lg[k]), per48(f"opp_{k}", lg[k])
    # Shrink toward the league a little: 10 games of evidence is half a vote.
    n = len(cur) + 0.5 * len(pri)
    for k in ("pts", "opp_pts", "reb", "opp_reb", "ast", "opp_ast", "fg3m", "opp_fg3m"):
        base = lg[k.replace("opp_", "")]
        prof[k] = regress(prof[k], n, base, 10)

    last = tg[0]["d"] if tg else None
    prof["rest_days"] = (date.fromisoformat(d) - date.fromisoformat(last)).days - 1 if last else None
    prof["b2b"] = prof["rest_days"] == 0
    return prof


def player_profile(db, pid, team, d, season, lg):
    """
    What one player brings: per-minute production (both seasons, recent games
    weighted up, shrunk toward his position), a base minutes figure that moves
    fast when his role changes, how often he actually gets in, and his recent
    PRA for the boost comparison.
    """
    games = [g for g in db.games(pid, d) if g["type"] in (2, 3) and g["s"] in (season, season - 1)]
    cur = [g for g in games if g["s"] == season]
    pri = [g for g in games if g["s"] == season - 1]
    cur_p = [g for g in cur if g["played"]]
    pri_p = [g for g in pri if g["played"]]
    if not cur_p and not pri_p:
        return preseason_profile(db, pid, team, d, season, lg)
    grp = pos_group(db.pos(pid))
    prior = lg["pm"][grp]

    wp = PRI_W * max(0.15, 1 - len(cur_p) / PRI_FADE_GAMES)
    ws = [(g, RATE_DECAY ** i) for i, g in enumerate(cur_p)] + [(g, wp * 0.985 ** i) for i, g in enumerate(pri_p)]
    wmin = sum(g["min"] * w for g, w in ws)
    rates = {}
    for k in STATS:
        raw = sum(g[k] * w for g, w in ws) / wmin if wmin > 0 else prior[k]
        rates[k] = regress(raw, wmin, prior[k], RATE_K[k])
    use = sum((g["fga"] + 0.44 * g["fta"] + g["tov"]) * w for g, w in ws) / wmin if wmin > 0 else 0.0

    # -- minutes: games with THIS team this season move fastest; a trade or a
    #    new season falls back to what he did elsewhere / last year
    team_cur = [g for g in cur if g["team"] == team]

    def wavg(lst, decay):
        ws_ = [decay ** i for i in range(len(lst))]
        return sum(g["min"] * w for g, w in zip(lst, ws_)) / sum(ws_) if lst else None
    m_team = None
    if team_cur:
        m_team = MIN_RECENT_W * wavg(team_cur[:8], MIN_RECENT_DECAY) + (1 - MIN_RECENT_W) * wavg(team_cur, 0.93)
    m_other = wavg(cur[:10], 0.85) if cur else None
    m_pri = _mean(g["min"] for g in pri[:25]) if pri else None
    fallback = m_other if (m_other is not None and not team_cur) else m_pri
    n = len(team_cur)
    if n >= 6 or (m_team is not None and fallback is None):
        base = m_team
    elif n > 0:
        base = (n / 6) * m_team + (1 - n / 6) * fallback
    else:
        base = fallback if fallback is not None else 0.0
    # Last season's minutes are a guess about this season's role; pull them
    # a little toward a bench role until he shows it again.
    if not cur:
        base = 0.92 * base + 0.08 * 16.0 if base > 16 else base

    recent = team_cur[:10] if len(team_cur) >= 3 else (cur + pri)[:10]
    played_recent = [g for g in recent if g["played"]]
    p_play = len(played_recent) / len(recent) if recent else 1.0
    p_play = max(p_play, 0.25)
    st_lst = [g for g in (team_cur if team_cur else cur_p + pri_p) if g["played"]][:5]
    st_rate = _mean(g["st"] for g in st_lst) if st_lst else 0.0

    pra = lambda g: g["pts"] + g["reb"] + g["ast"]
    l10 = (cur_p + (pri_p if len(cur_p) < 5 else []))[:10]
    seas = cur_p if len(cur_p) >= 5 else (cur_p + pri_p)[:40]
    return {
        "pid": pid, "name": db.name(pid), "pos": db.pos(pid) or grp, "grp": grp, "team": team,
        "rates": rates, "usage": use, "base_min": max(0.0, base or 0.0), "p_play": p_play,
        "min_when_playing": _mean(g["min"] for g in played_recent) if played_recent else (base or 0.0),
        "st_rate": st_rate, "gp_cur": len(cur_p), "gp_pri": len(pri_p),
        "l10": [pra(g) for g in l10], "l10_detail": [(g["d"], g["opp"], g["min"], g["pts"], g["reb"], g["ast"], g["fg3m"]) for g in l10],
        "l10_pra": _mean(pra(g) for g in l10) if l10 else None,
        "l5_min": _mean(g["min"] for g in played_recent[:5]) if played_recent else None,
        "season_pra": _mean(pra(g) for g in seas) if seas else None,
        "season_min": _mean(g["min"] for g in seas) if seas else None,
        "l10_stats": {k: _mean(g[k] for g in l10) for k in STATS} if l10 else {},
        "last_game": cur_p[0]["d"] if cur_p else (pri_p[0]["d"] if pri_p else None),
        "prev_team": (cur_p + pri_p)[0]["team"],
        "recent_team_presence": _presence(db, pid, team, d),
    }


def _presence(db, pid, team, d):
    """How many of the team's last five real games he played in."""
    real = [g for g in db.team_games(team, d) if g["type"] in (2, 3)][:5]
    return sum(1 for g in real if pid in g["played"])


def preseason_profile(db, pid, team, d, season, lg):
    """
    A rookie (or anyone back from overseas or a lost season) has no NBA games
    to go on. Left out, he is not just missing from the board -- his minutes
    get handed to his teammates and inflate them (2025-26 opening week had 43
    such appearances, 12 of them 20+ minutes). His preseason gives the role;
    the per-minute rate leans hard on the position average. No NBA average
    means no line proxy, so he never gets a boost or a MORE / LESS tag.
    """
    pre = [g for g in db.games(pid, d) if g["type"] == 1 and g["s"] == season]
    pre_p = [g for g in pre if g["played"]]
    if not pre_p:
        return None
    grp = pos_group(db.pos(pid))
    prior = {k: v * PRE_PRIOR_F for k, v in lg["pm"][grp].items()}
    mins = sum(g["min"] for g in pre_p)
    rates = {k: regress(sum(g[k] for g in pre_p) / mins if mins else prior[k], mins, prior[k], PRE_RATE_K)
             for k in STATS}
    m_play = PRE_MIN_F * _mean(g["min"] for g in pre_p)
    pra = lambda g: g["pts"] + g["reb"] + g["ast"]
    return {
        "pid": pid, "name": db.name(pid), "pos": db.pos(pid) or grp, "grp": grp, "team": team,
        "rates": rates, "usage": 0.0, "base_min": m_play * len(pre_p) / len(pre),
        "p_play": max(0.25, len(pre_p) / len(pre)), "min_when_playing": m_play,
        "st_rate": _mean(g["st"] for g in pre_p[:5]),
        "gp_cur": 0, "gp_pri": 0, "pre_only": True, "pre_gp": len(pre_p), "pre_pra": _mean(pra(g) for g in pre_p),
        "l10": [], "l10_detail": [], "l10_pra": None, "l5_min": _mean(g["min"] for g in pre_p[:5]),
        "season_pra": None, "season_min": None, "l10_stats": {}, "last_game": pre_p[0]["d"], "prev_team": team,
        "recent_team_presence": 0,
    }


def implied_points(line, T, O, lg, is_home):
    """Expected team points: Vegas when it has spoken, otherwise offense x defense / league."""
    m_t = T["pts"] * O["opp_pts"] / lg["pts"] + (HOME_PTS if is_home else -HOME_PTS)
    m_o = O["pts"] * T["opp_pts"] / lg["pts"] + (-HOME_PTS if is_home else HOME_PTS)
    if T.get("b2b"):
        m_t -= 1.2
    if O.get("b2b"):
        m_o -= 1.2
    src = "model"
    # Without a line the model's own margin stands (backtests preferred it
    # undamped); MODEL_MARGIN < 1 would pull it toward a pick'em.
    mid, half = (m_t + m_o) / 2, (m_t - m_o) / 2
    m_t, m_o = mid + MODEL_MARGIN * half, mid - MODEL_MARGIN * half
    imp_t, imp_o = m_t, m_o
    if line and line.get("total"):
        total = line["total"]
        sp = line.get("spread")
        if sp is not None:
            sp_t = sp if is_home else -sp
            imp_t, imp_o = (total - sp_t) / 2, (total + sp_t) / 2
        else:
            imp_t, imp_o = total * m_t / (m_t + m_o), total * m_o / (m_t + m_o)
        src = line.get("book") or "line"
    elif line and line.get("spread") is not None:
        sp_t = line["spread"] if is_home else -line["spread"]
        tot = m_t + m_o
        imp_t, imp_o = (tot - sp_t) / 2, (tot + sp_t) / 2
        src = "spread only"
    return imp_t, imp_o, m_t, m_o, src


def nb_sf(k, mu, var):
    """P(X >= k) for a negative binomial with this mean and variance (Poisson if var <= mu)."""
    if k <= 0:
        return 1.0
    if mu <= 0:
        return 0.0
    k = int(math.ceil(k))
    if var <= mu * 1.0001:
        p, cdf = math.exp(-mu), 0.0
        for i in range(k):
            cdf += p
            p *= mu / (i + 1)
        return clip(1 - cdf, 0.0, 1.0)
    p = mu / var
    r = mu * mu / (var - mu)
    pmf = p ** r
    cdf = 0.0
    for i in range(k):
        cdf += pmf
        pmf *= (i + r) / (i + 1) * (1 - p)
    return clip(1 - cdf, 0.0, 1.0)


def stat_var(stat, mu):
    a, c = VAR_MODEL[stat]
    return a * max(mu, 0.01) ** c


def p_at_least(x, mu, var):
    """
    P(X >= x) for an integer threshold. Half negative binomial, half normal:
    the NB alone is too right-skewed for NBA box scores (it put the median
    too low and under-called every Over by ~3 points in the backtest), the
    normal alone too symmetric at the low end.
    """
    sd = math.sqrt(max(var, 1e-6))
    return 0.5 * nb_sf(x, mu, var) + 0.5 * (1 - norm_cdf((x - 0.5 - mu) / sd))


def dist_quantile(q, mu, var):
    """Smallest integer x with P(X <= x) >= q."""
    x = 0
    while x < 200 and 1 - p_at_least(x + 1, mu, var) < q:
        x += 1
    return x


def project_team(team, opp, avail, out, T, O, lg, line, is_home):
    """
    Tonight's numbers for every available player on one side.

    1. Minutes: each player's base, then the team is filled back to 241.5
       minutes. Room to grow is base x (43 - base), so a backup at 18 soaks up
       more of a missing starter's time than the star at 36 does, and players
       at the missing man's position get extra weight.
    2. Blowout: a big spread trims the starters, the bench gets it back.
    3. Production: per-minute rate x minutes, then every stat is scaled so
       the team adds up to what it is
       expected to produce tonight -- points from the Vegas implied total,
       rebounds/assists/threes from both teams' tendencies and the pace
       Vegas implies. A star sitting raises everyone else's share; a star
       returning lowers it.
    """
    imp_t, imp_o, m_t, m_o, src = implied_points(line, T, O, lg, is_home)
    env = imp_t / m_t if m_t > 0 else 1.0
    pace = ((imp_t + imp_o) / (m_t + m_o)) if (m_t + m_o) > 0 else 1.0
    ot = TEAM_MINUTES / 240.0
    targets = {
        "pts": imp_t,
        "reb": T["reb"] * O["opp_reb"] / lg["reb"] * pace ** 0.5 * ot,
        "ast": T["ast"] * O["opp_ast"] / lg["ast"] * env * ot,
        "fg3m": T["fg3m"] * O["opp_fg3m"] / lg["fg3m"] * env * ot,
    }

    plist = [p for p in avail if p["base_min"] > 0]
    # -- minutes
    vacated = {}
    for p in out:
        if p and p["recent_team_presence"] >= 3:
            vacated[p["grp"]] = vacated.get(p["grp"], 0.0) + p["base_min"]
    vac_tot = sum(vacated.values())
    mins = {p["pid"]: p["base_min"] for p in plist}
    deficit = TEAM_MINUTES - sum(mins.values())

    def room_weight(p, short):
        b = mins[p["pid"]]
        if short:
            w = (b + FILL_FLOOR) ** FILL_ALPHA * max(0.0, MAX_MINUTES - b) ** FILL_BETA
            if vac_tot > 0:
                w *= 1 + FILL_POS * vacated.get(p["grp"], 0.0) / vac_tot
        else:
            # Too many minutes on the books (someone is back): the end of the
            # bench gives them up, not the starters.
            w = b * max(0.0, MAX_MINUTES - b) ** TRIM_BETA
        return w
    base_exp = dict(mins)
    for _ in range(4):
        if abs(deficit) < 0.5:
            break
        ws = {p["pid"]: room_weight(p, deficit > 0) for p in plist}
        W = sum(ws.values())
        if W <= 0:
            break
        for p in plist:
            mins[p["pid"]] = clip(mins[p["pid"]] + deficit * ws[p["pid"]] / W, 0.0, MAX_MINUTES)
        deficit = TEAM_MINUTES - sum(mins.values())

    # -- blowout and back-to-back trims on the starters, bench gets it back
    sp_t = None
    if line and line.get("spread") is not None:
        sp_t = line["spread"] if is_home else -line["spread"]
    else:
        sp_t = -(imp_t - imp_o)
    starters = sorted(plist, key=lambda p: -mins[p["pid"]])[:5]
    st_ids = {p["pid"] for p in starters}
    cut_f = clip(BLOWOUT_K * max(0.0, abs(sp_t) - BLOWOUT_FROM), 0.0, 0.10)
    if T.get("b2b"):
        cut_f = 1 - (1 - cut_f) * B2B_MIN
    if cut_f > 0:
        cut = sum(mins[i] * cut_f for i in st_ids)
        bench = [p for p in plist if p["pid"] not in st_ids]
        bw = sum(mins[p["pid"]] for p in bench)
        for i in st_ids:
            mins[i] *= 1 - cut_f
        for p in bench:
            mins[p["pid"]] += cut * mins[p["pid"]] / bw if bw > 0 else 0

    # -- production
    raw = {}
    for p in plist:
        raw[p["pid"]] = {k: p["rates"][k] * mins[p["pid"]] for k in STATS}
    # Who absorbs the gap between the players' raw sum and the team target:
    # in proportion to raw x (his share / average share)^ZETA. ZETA > 0 hands
    # more of a vacated role to the go-to players; when there is too much
    # production on the books, NORM_ZETA_DOWN < 0 makes the role players give
    # it up first (a star's usage is the stickiest thing in the sport).
    scale, pscale = {}, {i: {} for i in raw}
    for k in STATS:
        s = sum(raw[i][k] for i in raw)
        scale[k] = clip((targets[k] / s) ** NORM_GAMMA[k], 0.7, 1.4) if s > 0 else 1.0
        gap = s * scale[k] - s
        zeta = NORM_ZETA_UP if gap > 0 else NORM_ZETA_DOWN
        avg = s / len(raw) if raw else 0.0
        wts = {i: raw[i][k] * (max(0.05, raw[i][k] / avg) if avg > 0 else 1.0) ** zeta for i in raw}
        W = sum(wts.values())
        for i in raw:
            adj = gap * wts[i] / W if W > 0 else 0.0
            pscale[i][k] = clip((raw[i][k] + adj) / raw[i][k], 0.5, 1.8) if raw[i][k] > 0 else scale[k]

    rows = []
    for p in plist:
        pid = p["pid"]
        m_exp = mins[pid]
        # A prop on a player who does not get in is voided, so the board projects
        # the night he plays: expected minutes / the chance he plays at all. Minutes
        # handed to him because the team is short also make it likelier he
        # plays -- a fringe guy filling a starter's hole is not a coin flip.
        added = max(0.0, m_exp - base_exp.get(pid, m_exp))
        pp = min(1.0, p["p_play"] + added / max(8.0, p["min_when_playing"]))
        m_cond = min(MAX_MINUTES, m_exp / pp) if pp > 0 else m_exp
        f = m_cond / m_exp if m_exp > 0 else 1.0
        mu = {k: raw[pid][k] * pscale[pid][k] * f for k in STATS}
        rows.append({
            **p, "opp": opp, "is_home": is_home, "min_exp": m_exp, "min": m_cond,
            "starter_tonight": pid in st_ids,
            "mu": mu, "pra": mu["pts"] + mu["reb"] + mu["ast"],
            "scale": scale, "env": env, "pace": pace, "imp": imp_t, "imp_opp": imp_o,
            "spread_t": sp_t, "line_src": src, "targets": targets,
            "blowout_cut": cut_f,
            "vacated": vacated, "min_delta": m_cond - (p["l5_min"] or p["base_min"]),
        })
    return rows


def finish_row(r):
    """Distributions, ladders, grade and boost for one projected player."""
    mu = r["pra"]
    # Until he has five games this season the last-10 is last April -- rest
    # days and tank jobs -- so the season-long average stands in.
    ref = r["l10_pra"] if r["gp_cur"] >= 5 else r["season_pra"]
    r["ref_pra"] = ref
    dk = r.get("dk_line")
    if dk is not None:
        # DraftKings' own line. The boost is the model's disagreement with it;
        # the odds are built mostly from the line (see DK_MODEL_W).
        r["line_src"], r["proxy_line"] = "DK", dk
        r["boost"] = mu - dk
        p_mean = dk + DK_MODEL_W * (mu - dk)
        var = stat_var("pra", p_mean)
    else:
        # No DK line (deep bench, or props not posted yet): his recent average
        # to the half point stands in, about where a book with no news would
        # hang it. A projection far from it is a bolder call, so the spread
        # widens with the boost.
        r["line_src"] = "avg"
        r["proxy_line"] = (math.floor(ref) + 0.5) if ref is not None else None
        r["boost"] = (mu - ref) if ref is not None else 0.0
        p_mean = mu
        var = stat_var("pra", mu) + BOOST_VAR_K * r["boost"] ** 2
    r["p_mean"], r["sd"], r["var"] = p_mean, math.sqrt(var), var
    r["floor"] = dist_quantile(0.25, p_mean, var)
    r["ceiling"] = dist_quantile(0.75, p_mean, var)
    r["var_s"] = {k: stat_var(k, r["mu"][k]) for k in STATS}
    base = 5 * round(p_mean / 5)
    r["ladder"] = [(x, p_at_least(x, p_mean, var)) for x in (base - 10, base - 5, base, base + 5, base + 10) if x > 0]
    r["p_over_proxy"] = (p_at_least(math.ceil(r["proxy_line"]), p_mean, var)
                         if r["proxy_line"] is not None else None)
    r["p_pts20"] = p_at_least(20, r["mu"]["pts"], r["var_s"]["pts"])
    r["p_3pm2"] = p_at_least(2, r["mu"]["fg3m"], r["var_s"]["fg3m"])
    r["p_reb10"] = p_at_least(10, r["mu"]["reb"], r["var_s"]["reb"])
    r["p_ast8"] = p_at_least(8, r["mu"]["ast"], r["var_s"]["ast"])
    r["grade"] = grade_for(mu, PRA_GRADES)
    r["grade_color"] = GRADE_COLORS[r["grade"]]
    r["scout"] = scout_note(r)
    return r


def project_slate(db, games, d, season, rosters, out_ids, lines, injuries=None, dk=None):
    """
    Project every game. rosters: {team: [pid]}; out_ids: set of pids ruled out;
    lines: {game_id: odds dict or None}; dk: {game_id: {pid: [PRA line, open]}}.
    Returns (rows, team_profiles, lg).
    """
    lg = league_baselines(db, d, season)
    tprof = {}
    for g in games:
        for t in (g["home"], g["away"]):
            if t not in tprof:
                tprof[t] = team_profile(db, t, d, season, lg)
    rows = []
    for g in games:
        for team, opp, is_home in ((g["home"], g["away"], True), (g["away"], g["home"], False)):
            avail, out = [], []
            for pid in rosters.get(team, []):
                p = player_profile(db, pid, team, d, season, lg)
                if p is None:
                    continue
                (out if pid in out_ids else avail).append(p)
            proj = project_team(team, opp, avail, out, tprof[team], tprof[opp], lg, lines.get(g["game_id"]), is_home)
            names_out = [p["name"] for p in sorted(out, key=lambda x: -x["base_min"])
                         if p["recent_team_presence"] >= 3 and p["base_min"] >= 15]
            for r in proj:
                r.update({"game_id": g["game_id"], "start_str": g.get("start_str", ""),
                          "main_slate": g.get("main_slate", True), "b2b": tprof[team]["b2b"],
                          "opp_b2b": tprof[opp]["b2b"], "rest_days": tprof[team]["rest_days"],
                          "mates_out": names_out, "total": (lines.get(g["game_id"]) or {}).get("total")})
                dkl = ((dk or {}).get(g["game_id"]) or {}).get(r["pid"])
                r["dk_line"], r["dk_open"] = (dkl[0], dkl[1]) if dkl else (None, None)
                inj = (injuries or {}).get(r["pid"])
                r["inj_status"] = inj["status"] if inj else ""
                r["inj_comment"] = inj["comment"] if inj else ""
                r["inj_note"] = inj["note"] if inj else ""
                rows.append(finish_row(r))
            for p in out:
                inj = (injuries or {}).get(p["pid"], {})
                rows.append({**p, "opp": opp, "is_home": is_home, "game_id": g["game_id"],
                             "start_str": g.get("start_str", ""), "main_slate": g.get("main_slate", True),
                             "grade": "OUT", "grade_color": GRADE_COLORS["OUT"], "pra": 0.0, "boost": 0.0,
                             "inj_status": inj.get("status", "Out"), "inj_comment": inj.get("comment", ""),
                             "inj_note": inj.get("note", ""), "out": True})
    return rows, tprof, lg


def _short(name):
    parts = name.split()
    return f"{parts[0][0]}. {' '.join(parts[1:])}" if len(parts) > 1 else name


def scout_note(r):
    bits = []
    if r.get("mates_out"):
        who = ", ".join(_short(n) for n in r["mates_out"][:3])
        if r["scale"]["pts"] >= 1.05 or r["min_delta"] >= 2:
            bits.append(f"{who} out: {r['min']:.0f} min ({r['min_delta']:+.1f}), team shares up {(r['scale']['pts'] - 1) * 100:+.0f}%")
        else:
            bits.append(f"{who} out")
    elif r["min_delta"] >= 3:
        bits.append(f"minutes up: {r['min']:.0f} projected vs {r['l5_min'] or 0:.0f} last 5")
    if not r.get("mates_out") and r["scale"]["pts"] <= 0.92:
        bits.append(f"crowded offense: {r['imp']:.0f} implied points to go around (team shares {(r['scale']['pts'] - 1) * 100:+.0f}%)")
    elif not r.get("mates_out") and r["scale"]["pts"] >= 1.08:
        bits.append(f"thin rotation: team shares {(r['scale']['pts'] - 1) * 100:+.0f}% to reach {r['imp']:.0f} implied points")
    elif r["min_delta"] <= -3:
        bits.append(f"minutes down: {r['min']:.0f} projected vs {r['l5_min'] or 0:.0f} last 5 (crowded rotation)")
    if r.get("total"):
        if r["total"] >= 236:
            bits.append(f"shootout: total {r['total']:.1f}")
        elif r["total"] <= 216:
            bits.append(f"slow game: total {r['total']:.1f}")
    if r.get("blowout_cut", 0) >= 0.03 and r["starter_tonight"]:
        bits.append(f"blowout risk ({abs(r['spread_t']):.1f}-pt spread) trims starters")
    if r.get("b2b"):
        bits.append("2nd of a back-to-back")
    if r.get("l10") and len(r["l10"]) >= 5 and r.get("season_pra"):
        l5 = _mean(r["l10"][:5])
        if l5 >= r["season_pra"] * 1.15:
            bits.append(f"hot: {l5:.1f} PRA last 5 vs {r['season_pra']:.1f}")
        elif l5 <= r["season_pra"] * 0.85:
            bits.append(f"cold: {l5:.1f} PRA last 5 vs {r['season_pra']:.1f}")
    if r.get("pre_only"):
        bits.append(f"no NBA games yet: role from {r['pre_gp']} preseason games ({r['pre_pra']:.1f} PRA, "
                    f"{r['l5_min']:.0f} min) -- no line to compare against")
    elif r["gp_cur"] == 0 and r.get("prev_team") and r["prev_team"] != r["team"]:
        bits.append(f"new team: role assumed from {r['prev_team']}")
    if r.get("inj_status") and not r.get("out"):
        bits.append(f"{r['inj_status']}{(': ' + r['inj_note']) if r.get('inj_note') else ''}")
    return " · ".join(bits) or "Nothing unusual tonight -- this is his baseline."


# ============================================================
#  RESULTS TRACKING
# ============================================================

def _pred_path(slate_date, backtest=False):
    return os.path.join(PREDICTIONS_FOLDER, f"{'backtest' if backtest else 'pred'}_{slate_date.isoformat()}.json")


def _gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}


def _repo_get(path):
    """Fetch a file from the GitHub repo. Returns (bytes, sha) or (None, None)."""
    import base64
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{path}",
                         headers=_gh_headers(), timeout=15)
        if r.status_code != 200:
            return None, None
        j = r.json()
        return base64.b64decode(j["content"]), j["sha"]
    except Exception:
        return None, None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sync_predictions_down(slate_date):
    """
    Pull tonight's log from the repo before doing anything. The tool runs from
    two places -- the PC and GitHub Actions -- and if each kept its own log
    they would drift. The repo copy is the single record.
    """
    if not GITHUB_TOKEN:
        return
    fname = os.path.basename(_pred_path(slate_date))
    data, _ = _repo_get(f"{PREDICTIONS_FOLDER}/{fname}")
    if data is None:
        return
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    local = os.path.join(PREDICTIONS_FOLDER, fname)
    try:
        remote = json.loads(data)
        mine = _read_json(local)
        if mine and mine.get("logged_at", "") >= remote.get("logged_at", ""):
            return
        with open(local, "wb") as f:
            f.write(data)
        print(f"  Synced {fname} from repo (logged {remote.get('logged_at', '?')[:16]})")
    except Exception as e:
        print(f"  [!] Prediction sync: {e}")


def sync_predictions_up(path):
    """Push a prediction log back to the repo so every machine sees it."""
    if not GITHUB_TOKEN or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        content = f.read()
    rel = path.replace("\\", "/")
    if not deploy_file(content, rel, _gh_headers(), f"Predictions {os.path.basename(path)}"):
        print(f"  [!] Could not push {rel} to repo")


def logged_out(slate_date):
    data = _read_json(_pred_path(slate_date))
    if not data:
        return None
    return {r["name"] for r in data.get("predictions", []) if r.get("grade") == "OUT"}


def log_predictions(slate_date, rows, games, preseason=False, backtest=False):
    """
    Save tonight's calls so they can be graded tomorrow. Rewritten on every
    run so the stored file is the LAST read before tip -- except per game:
    once a game has started its rows are frozen. Logging a "prediction" after
    the result is known would make the accuracy numbers meaningless.
    """
    os.makedirs(PREDICTIONS_FOLDER, exist_ok=True)
    path = _pred_path(slate_date, backtest)
    done_teams = set() if backtest else {t for g in games if g["started"] or g["final"] for t in (g["home"], g["away"])}
    old = _read_json(path)
    prior = {r["pid"]: r for r in old.get("predictions", [])} if old else {}

    out, kept = [], 0
    for r in rows:
        if r["team"] in done_teams and r["pid"] in prior:
            out.append(prior[r["pid"]])
            kept += 1
            continue
        row = {"pid": r["pid"], "name": r["name"], "pos": r["grp"], "team": r["team"], "opp": r["opp"],
               "game_id": r["game_id"], "grade": r["grade"], "main": r.get("main_slate", True)}
        if r["grade"] != "OUT":
            row.update({"pra": round(r["pra"], 2), "pts": round(r["mu"]["pts"], 2), "reb": round(r["mu"]["reb"], 2),
                        "ast": round(r["mu"]["ast"], 2), "fg3m": round(r["mu"]["fg3m"], 2),
                        "min": round(r["min"], 1), "ref": round(r["ref_pra"], 2) if r["ref_pra"] is not None else None,
                        "boost": round(r["boost"], 2), "proxy": r["proxy_line"], "line_src": r["line_src"],
                        "p_over": round(r["p_over_proxy"], 4) if r["p_over_proxy"] is not None else None,
                        "var": round(r["var"], 2)})
        if r["team"] in done_teams:
            row["late"] = True           # game already under way and never logged: not a real call
        out.append(row)

    data = {"date": slate_date.isoformat(), "preseason": preseason, "backtest": backtest,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
            "games": [{"game_id": g["game_id"], "home": g["home"], "away": g["away"],
                       "start_str": g.get("start_str", "")} for g in games],
            "predictions": out}
    if old and old.get("scored"):
        data["scored"] = old["scored"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    msg = f"  Logged {len(out)} calls -> {path}"
    if kept:
        msg += f" ({kept} locked -- games under way or final)"
    print(msg)
    return path


BOOST_CALL = 3.0      # a projection this far from the DK line counts as a More / Less call


def score_date(slate_date, backtest=False, force=False):
    """
    Grade one logged slate against the box scores.

    Four questions, each easy to fool yourself on alone:
      * Is the projection close? (PRA error, and is it biased high or low)
      * Do the grades sort players? (A+ should out-produce B)
      * Is the model closer than DraftKings' own line? (both errors, same players)
      * Do the model's disagreements with DK win? A More call (3+ above the DK
        line) wins when he clears it; a Less call (3+ below) when he does not.
        A coin flip is 50%, and a pick needs well above that to pay.
    Only real DK lines count for the last two. A player who never got in is
    skipped -- a prop on a DNP is voided.
    """
    path = _pred_path(slate_date, backtest)
    data = _read_json(path)
    if not data:
        return None
    if data.get("scored") and not force:
        return data["scored"]
    season = season_of(slate_date)
    actual, finished = {}, set()
    for g in data.get("games", []):
        b = get_box(g["game_id"], season)
        if not b:
            continue
        finished.update((b["home"], b["away"]))
        for r in b["p"]:
            actual[r[0]] = {"min": r[C["min"]], **{k: r[C[k]] for k in STATS},
                            "pra": r[C["pts"]] + r[C["reb"]] + r[C["ast"]]}
    if not finished:
        return None

    tiers, err, bias, min_err, brier = {}, [], [], [], []
    vs_dk = {"n": 0, "model": 0.0, "dk": 0.0, "over": 0}
    more = {"n": 0, "w": 0}
    less = {"n": 0, "w": 0}
    dnp = 0
    best = []
    for p in data["predictions"]:
        if p.get("late") or p["grade"] == "OUT" or p["team"] not in finished:
            continue
        a = actual.get(p["pid"])
        if not a or a["min"] <= 0:
            dnp += 1
            continue
        if p.get("min", 0) < MIN_PROJ_MINUTES:
            continue
        t = tiers.setdefault(p["grade"], {"n": 0, "proj": 0.0, "act": 0.0, "err": 0.0})
        t["n"] += 1
        t["proj"] += p["pra"]
        t["act"] += a["pra"]
        t["err"] += abs(a["pra"] - p["pra"])
        err.append(abs(a["pra"] - p["pra"]))
        bias.append(a["pra"] - p["pra"])
        min_err.append(abs(a["min"] - p["min"]))
        if p.get("line_src") == "DK" and p.get("proxy") is not None and p.get("p_over") is not None:
            over = 1 if a["pra"] > p["proxy"] else 0
            vs_dk["n"] += 1
            vs_dk["model"] += abs(a["pra"] - p["pra"])
            vs_dk["dk"] += abs(a["pra"] - p["proxy"])
            vs_dk["over"] += over
            brier.append((p["p_over"] - over) ** 2)
            if p["boost"] >= BOOST_CALL:
                more["n"] += 1
                more["w"] += over
                best.append({"name": p["name"], "team": p["team"], "call": "More", "line": p["proxy"],
                             "pra": p["pra"], "act": a["pra"], "won": bool(over)})
            elif p["boost"] <= -BOOST_CALL:
                less["n"] += 1
                less["w"] += 1 - over
    n = len(err)
    if not n:
        return None
    for t in tiers.values():
        for k in ("proj", "act", "err"):
            t[k] = round(t[k] / t["n"], 2)
    all_teams = {t for g in data.get("games", []) for t in (g["home"], g["away"])}
    games_left = len(all_teams - finished) // 2
    result = {
        "date": slate_date.isoformat(), "n": n, "dnp": dnp,
        "mae": round(_mean(err), 2), "bias": round(_mean(bias), 2), "min_mae": round(_mean(min_err), 2),
        "brier": round(_mean(brier), 4) if brier else None,
        "more": more, "less": less, "tiers": tiers, "vs_dk": vs_dk,
        "partial": games_left > 0, "games_left": games_left,
        "top_more": sorted(best, key=lambda x: -(x["pra"] - x["line"]))[:8],
        "preseason": data.get("preseason", False),
    }
    if not result["partial"]:
        data["scored"] = result
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            if not backtest:
                sync_predictions_up(path)
        except Exception as e:
            print(f"  [!] Could not store scorecard: {e}")
    return result


def accuracy_history(slate_date, days=14):
    """Every scored slate in the window, newest first, plus an all-time roll-up (regular season only)."""
    recent = []
    for i in range(1, days + 1):
        r = score_date(slate_date - timedelta(days=i))
        if r:
            recent.append(r)
    total = {"n": 0, "err": 0.0, "bias": 0.0, "days": 0, "more": {"n": 0, "w": 0}, "less": {"n": 0, "w": 0},
             "tiers": {}, "vs_dk": {"n": 0, "model": 0.0, "dk": 0.0, "over": 0}}
    files = []
    if os.path.isdir(PREDICTIONS_FOLDER):
        files = sorted(f for f in os.listdir(PREDICTIONS_FOLDER) if f.startswith("pred_") and f.endswith(".json"))
    for f in files:
        d = f[5:15]
        r = next((x for x in recent if x["date"] == d), None)
        if r is None:
            r = (_read_json(os.path.join(PREDICTIONS_FOLDER, f)) or {}).get("scored")
        if not r or r.get("preseason"):
            continue
        total["n"] += r["n"]
        total["err"] += r["mae"] * r["n"]
        total["bias"] += r["bias"] * r["n"]
        total["days"] += 1
        for k in ("more", "less"):
            total[k]["n"] += r[k]["n"]
            total[k]["w"] += r[k]["w"]
        for k, v in (r.get("vs_dk") or {}).items():
            total["vs_dk"][k] += v
        for g, t in r["tiers"].items():
            a = total["tiers"].setdefault(g, {"n": 0, "proj": 0.0, "act": 0.0})
            a["n"] += t["n"]
            a["proj"] += t["proj"] * t["n"]
            a["act"] += t["act"] * t["n"]
    if total["n"]:
        total["mae"] = round(total["err"] / total["n"], 2)
        total["bias"] = round(total["bias"] / total["n"], 2)
        for t in total["tiers"].values():
            t["proj"] = round(t["proj"] / t["n"], 1)
            t["act"] = round(t["act"] / t["n"], 1)
    return recent, total


# ============================================================
#  HTML DASHBOARD
# ============================================================

CSS = """
* { box-sizing:border-box; margin:0; padding:0; }
body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',system-ui,sans-serif; line-height:1.5; }
a { color:#ffa657; }

#splash { position:fixed; inset:0; z-index:9999; display:flex; flex-direction:column; align-items:center; justify-content:center;
  background:radial-gradient(ellipse at center,#2a1505 0%,#120a03 60%,#000 100%); transition:opacity .8s ease; }
#splash.fade-out { opacity:0; pointer-events:none; }
.splash-title { font-size:clamp(1.5rem,7.5vw,2.8rem); font-weight:900; letter-spacing:clamp(2px,1vw,6px); text-align:center; color:#ffa657; text-shadow:0 0 40px #ffa65788; animation:glow 2s ease-in-out infinite alternate; }
.splash-sub { font-size:1rem; color:#8b949e; letter-spacing:3px; margin-top:8px; text-transform:uppercase; }
.ball { width:64px; height:64px; border-radius:50%; background:radial-gradient(circle at 35% 35%,#f0883e,#c2410c 70%); box-shadow:0 8px 20px #000a; margin-bottom:26px; position:relative; animation:bounce .7s cubic-bezier(.5,0,.5,1) infinite alternate; }
.ball:before, .ball:after { content:''; position:absolute; inset:0; border-radius:50%; border:2px solid #3b1a06; }
.ball:before { clip-path:inset(0 48% 0 48%); } .ball:after { clip-path:inset(48% 0 48% 0); }
@keyframes glow { from { text-shadow:0 0 20px #ffa65766; } to { text-shadow:0 0 60px #ffa657cc,0 0 100px #ffa65744; } }
@keyframes bounce { from { transform:translateY(-40px); } to { transform:translateY(10px) scaleY(.92); } }

.header { background:linear-gradient(135deg,#2a1505,#0d1117 70%); padding:34px 20px; text-align:center; border-bottom:2px solid #db6d28; }
.header h1 { font-size:2.5rem; color:#ffa657; letter-spacing:3px; font-weight:800; }
.header p { color:#8b949e; margin-top:6px; font-size:14px; }
.header .lock { color:#e6edf3; font-weight:600; }
.banner { background:#3d2e00; border:1px solid #9e6a03; color:#f2cc60; padding:10px 16px; border-radius:8px; margin:18px auto 0; max-width:1100px; font-size:13px; }

.container { max-width:1100px; margin:0 auto; padding:24px 14px; }
h2.section-title { color:#ffa657; font-size:1.15rem; margin:34px 0 14px; border-bottom:1px solid #21262d; padding-bottom:8px; display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; }
h2.section-title small { color:#8b949e; font-size:12px; font-weight:400; }

.toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:14px; }
.toggle { background:#161b22; border:1px solid #30363d; color:#c9d1d9; padding:7px 13px; border-radius:8px; cursor:pointer; font-size:13px; font-family:inherit; }
.toggle.on { border-color:#f0883e; color:#ffa657; background:#2a1505; }
.sep { width:1px; height:24px; background:#30363d; margin:0 4px; }
.games-row { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:22px; }
.game-chip { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:11px 14px; min-width:180px; cursor:pointer; user-select:none; transition:border-color .15s,background .15s; }
.game-chip:hover { border-color:#db6d28; background:#1c2128; }
.game-chip.active { border-color:#f0883e; background:#2a1505; box-shadow:0 0 0 1px #f0883e55; }
.game-chip .teams { font-size:1rem; font-weight:700; display:flex; align-items:center; gap:6px; }
.game-chip img { width:22px; height:22px; }
.game-chip .time { font-size:12px; color:#8b949e; margin-top:2px; }
.game-chip .meta { font-size:11px; color:#6e7681; margin-top:2px; }
.filter-banner { display:none; background:#2a1505; border:1px solid #f0883e55; border-radius:8px; padding:9px 14px; margin-bottom:18px; color:#ffa657; font-size:13px; font-weight:600; }

.about { margin:0 0 22px; }
.about-bar { width:100%; display:flex; justify-content:space-between; align-items:center; gap:12px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:12px 16px; color:#c9d1d9; font-size:13px; text-align:left; cursor:pointer; font-family:inherit; }
.about-bar:hover { border-color:#f0883e; }
.about-body { display:none; background:#0f141b; border:1px solid #30363d; border-top:none; border-radius:0 0 12px 12px; padding:14px 18px; font-size:13px; color:#c9d1d9; }
.about.open .about-body { display:block; } .about.open .about-bar { border-radius:12px 12px 0 0; }
.about-body p { margin:6px 0; } .about-body b { color:#ffa657; }

.edges { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:12px; margin-bottom:8px; }
.edge-box { background:#161b22; border:1px solid #30363d; border-radius:12px; padding:12px 14px; }
.edge-box h3 { font-size:13px; letter-spacing:1px; text-transform:uppercase; margin-bottom:6px; }
.edge-box table { width:100%; border-collapse:collapse; font-size:13px; }
.edge-box td { padding:4px 4px; border-bottom:1px solid #21262d; white-space:nowrap; }
.edge-box td.r { text-align:right; } .edge-box td.why { white-space:normal; color:#8b949e; font-size:11px; }
.edge-box tr { cursor:pointer; } .edge-box tr:hover td { background:#1c2128; }

.player-card { display:flex; gap:12px; background:#161b22; border:1px solid #30363d; border-radius:12px; padding:14px; margin-bottom:12px; align-items:stretch; }
.player-card.out { opacity:.55; }
.card-rank { min-width:64px; display:flex; flex-direction:column; align-items:center; justify-content:center; border-radius:10px; font-size:13px; font-weight:700; padding:6px; }
.card-body { flex:1; min-width:0; }
.card-name { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 10px; }
.card-name .nm { font-size:1.1rem; font-weight:700; }
.card-name .ctx { color:#8b949e; font-size:13px; }
.tag { font-size:11px; font-weight:700; padding:1px 7px; border-radius:6px; border:1px solid; }
.tag.st { color:#ffa657; border-color:#ffa65766; background:#ffa65711; }
.tag.bn { color:#c9d1d9; border-color:#30363d; }
.tag.dtd { color:#f2cc60; border-color:#f2cc60; } .tag.out { color:#ff7b72; border-color:#ff7b72; }
.tag.up { color:#3fb950; border-color:#3fb95088; } .tag.down { color:#ff7b72; border-color:#ff7b7288; }
.card-label { font-size:14px; font-weight:600; margin:4px 0 8px; }
.card-stats { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:8px 14px; }
.stat-label { font-size:10px; text-transform:uppercase; letter-spacing:1px; color:#6e7681; }
.stat-val { font-size:13px; color:#e6edf3; display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
.stat-sub { font-size:11px; color:#8b949e; }
.bar { display:inline-block; width:70px; height:7px; background:#21262d; border-radius:4px; overflow:hidden; vertical-align:middle; }
.bar i { display:block; height:100%; border-radius:4px; }
.ladder { display:flex; gap:4px; flex-wrap:wrap; }
.ladder span { font-size:11px; padding:1px 6px; border-radius:5px; background:#21262d; color:#c9d1d9; }
.checker { display:flex; align-items:center; gap:8px; margin-top:9px; flex-wrap:wrap; font-size:12px; color:#8b949e; }
.checker input { width:76px; background:#0d1117; border:1px solid #30363d; color:#e6edf3; border-radius:6px; padding:4px 6px; font-family:inherit; font-size:13px; }
.checker input:focus { outline:none; border-color:#f0883e; }
.checker .res { font-weight:700; }
.card-scout { margin-top:9px; font-size:12px; color:#c9d1d9; border-top:1px dashed #30363d; padding-top:7px; }
.card-score { min-width:64px; display:flex; flex-direction:column; align-items:center; justify-content:center; font-size:1.4rem; font-weight:800; }
.card-score small { font-size:11px; font-weight:600; color:#8b949e; }
.show-all { background:#161b22; border:1px dashed #30363d; color:#8b949e; width:100%; padding:10px; border-radius:10px; cursor:pointer; font-family:inherit; font-size:13px; margin:2px 0 8px; }
.show-all:hover { color:#e6edf3; border-color:#f0883e; }
.empty { color:#8b949e; font-size:13px; padding:12px; background:#161b22; border-radius:10px; border:1px solid #30363d; }

table.score { width:100%; border-collapse:collapse; font-size:13px; margin-bottom:14px; }
table.score th, table.score td { padding:7px 8px; border-bottom:1px solid #21262d; text-align:right; white-space:nowrap; }
table.score th:first-child, table.score td:first-child { text-align:left; }
table.score th { color:#8b949e; font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:1px; }
.scroll { overflow-x:auto; }
.good { color:#3fb950; } .bad { color:#ff7b72; } .muted { color:#8b949e; }
.footer { text-align:center; color:#6e7681; font-size:12px; padding:30px 0 40px; }
@media (max-width:600px) { .card-score { display:none; } .card-rank { min-width:54px; } .header h1 { font-size:1.7rem; letter-spacing:2px; } }
"""

JS = """
window.addEventListener('load', function () {
  setTimeout(function () { var s = document.getElementById('splash'); if (s) s.classList.add('fade-out'); }, 900);
  try {
    var about = document.getElementById('about');
    if (!localStorage.getItem('bg_about_seen')) { about.classList.add('open'); localStorage.setItem('bg_about_seen', '1'); }
  } catch (e) {}
});
function toggleAbout() { document.getElementById('about').classList.toggle('open'); }
var activeGame = null, mainOnly = false, posFilter = 'ALL', showAllCards = false, sortKey = 'pra';
function applyFilters() {
  var board = document.getElementById('board');
  var cards = Array.prototype.slice.call(board.querySelectorAll('.player-card'));
  cards.sort(function (a, b) {
    var oa = a.dataset.out === '1', ob = b.dataset.out === '1';
    if (oa !== ob) return oa ? 1 : -1;
    if (sortKey === 'boost') return parseFloat(b.dataset.boost) - parseFloat(a.dataset.boost);
    if (sortKey === 'fade') return parseFloat(a.dataset.boost) - parseFloat(b.dataset.boost);
    return parseFloat(b.dataset.pra) - parseFloat(a.dataset.pra);
  });
  var shown = 0, n = 0, more = 0;
  cards.forEach(function (c) {
    board.appendChild(c);
    var ok = (!activeGame || activeGame.indexOf(c.dataset.team) >= 0) && (!mainOnly || c.dataset.main === '1') &&
             (posFilter === 'ALL' || c.dataset.pos === posFilter) &&
             (c.dataset.rot === '1' || showAllCards || activeGame);
    if (!ok && c.dataset.rot !== '1' && !showAllCards) more += 1;
    var limit = showAllCards || activeGame ? 1e9 : TOP_SHOWN;
    if (ok) { n += 1; if (n > limit) { ok = false; more += 1; } }
    c.style.display = ok ? '' : 'none';
    if (ok) { shown += 1; c.querySelector('.rk').textContent = '#' + shown; }
  });
  var btn = document.getElementById('show-all');
  btn.style.display = more > 0 && !showAllCards ? '' : 'none';
  btn.textContent = 'Show all ' + (shown + more) + ' players (' + more + ' more, incl. deep bench and OUT)';
  document.querySelectorAll('.game-chip[data-teams]').forEach(function (g) {
    g.style.display = (!mainOnly || g.dataset.main === '1') ? '' : 'none';
  });
  var b = document.getElementById('filter-banner');
  if (activeGame) { b.style.display = 'block'; b.textContent = 'Showing ' + activeGame.join(' vs ') + ' only -- tap the game again or "All games" to clear.'; }
  else { b.style.display = 'none'; }
}
function pickGame(el) {
  var teams = el.dataset.teams ? el.dataset.teams.split(',') : null;
  document.querySelectorAll('.game-chip').forEach(function (g) { g.classList.remove('active'); });
  if (teams && activeGame && teams[0] === activeGame[0]) { activeGame = null; }
  else { activeGame = teams; el.classList.add('active'); }
  if (!activeGame) { var all = document.getElementById('all-chip'); if (all) all.classList.add('active'); }
  applyFilters();
}
function toggleMain(el) { mainOnly = !mainOnly; el.classList.toggle('on', mainOnly); applyFilters(); }
function setPos(el, p) {
  posFilter = p; document.querySelectorAll('.pos-btn').forEach(function (b) { b.classList.remove('on'); });
  el.classList.add('on'); applyFilters();
}
function setSort(el, k) {
  sortKey = k; document.querySelectorAll('.sort-btn').forEach(function (b) { b.classList.remove('on'); });
  el.classList.add('on'); applyFilters();
}
function showAll(btn) { showAllCards = true; applyFilters(); }
function jumpTo(pid) {
  var c = document.getElementById('p-' + pid); if (!c) return;
  c.style.display = ''; c.scrollIntoView({behavior: 'smooth', block: 'center'});
  c.style.boxShadow = '0 0 0 2px #f0883e'; setTimeout(function () { c.style.boxShadow = ''; }, 1600);
}
// Half negative binomial, half normal -- the same distribution the board's ladders use.
function nbSf(k, mu, v) {
  k = Math.ceil(k); if (k <= 0) return 1; if (mu <= 0) return 0;
  var cdf = 0, i;
  if (v <= mu * 1.0001) { var p = Math.exp(-mu); for (i = 0; i < k; i++) { cdf += p; p *= mu / (i + 1); } return Math.max(0, Math.min(1, 1 - cdf)); }
  var q = mu / v, r = mu * mu / (v - mu), pmf = Math.pow(q, r);
  for (i = 0; i < k; i++) { cdf += pmf; pmf *= (i + r) / (i + 1) * (1 - q); }
  return Math.max(0, Math.min(1, 1 - cdf));
}
function normCdf(z) {
  var t = 1 / (1 + 0.3275911 * Math.abs(z) / Math.SQRT2);
  var y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z / 2);
  return z >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y);
}
function checkLine(inp) {
  var out = inp.parentNode.querySelector('.res');
  var line = parseFloat(inp.value);
  if (isNaN(line)) { out.textContent = ''; return; }
  var c = inp.closest('.player-card');
  var mu = parseFloat(c.dataset.pm), v = parseFloat(c.dataset.var);
  var over = 0.5 * nbSf(Math.floor(line) + 1, mu, v) + 0.5 * (1 - normCdf((Math.floor(line) + 0.5 - mu) / Math.sqrt(v)));
  var more = over >= 0.5;
  out.textContent = (more ? 'More ' + Math.round(over * 100) : 'Less ' + Math.round((1 - over) * 100)) + '%';
  out.style.color = Math.abs(over - 0.5) >= 0.08 ? (more ? '#3fb950' : '#ff7b72') : '#c9d1d9';
}
"""


def _bar(value, max_value, color):
    pct = int(clip(value / max_value if max_value else 0, 0, 1) * 100)
    return f'<span class="bar"><i style="width:{pct}%;background:{color}"></i></span>'


def _esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _logo(t):
    return f'<img src="https://a.espncdn.com/i/teamlogos/nba/500/{t.lower()}.png" alt="{t}" loading="lazy">'


def _inj_tag(p):
    s = p.get("inj_status", "")
    if not s:
        return ""
    title = _esc(p.get("inj_comment", ""))
    if p.get("out"):
        return f'<span class="tag out" title="{title}">OUT{": " + _esc(p["inj_note"]) if p.get("inj_note") else ""}</span>'
    return f'<span class="tag dtd" title="{title}">{_esc(s)}{(" - " + _esc(p["inj_note"])) if p.get("inj_note") else ""}</span>'


def _player_card(p):
    team_color = display_color(TEAMS.get(p["team"], ("", "", "#8b949e"))[2])
    home_away = "vs" if p["is_home"] else "@"
    style = f' style="border-left:4px solid {team_color}"'
    if p.get("out"):
        return f"""
    <div class="player-card out" id="p-{p['pid']}"{style} data-team="{p['team']}" data-main="{1 if p.get('main_slate', True) else 0}" data-pos="{p['grp']}" data-pra="0" data-pm="0" data-boost="0" data-var="1" data-out="1" data-rot="0">
      <div class="card-rank" style="background:#ff444422;color:#ff4444;border:1px solid #ff444444"><span class="rk">#</span><strong style="font-size:1.1rem">OUT</strong></div>
      <div class="card-body">
        <div class="card-name"><span class="nm" style="color:{team_color}">{_esc(p['name'])}</span>
          <span class="ctx">{p['grp']} &bull; {p['team']} {home_away} {p['opp']}</span>{_inj_tag(p)}</div>
        <div class="card-label" style="color:#ff7b72">Not playing &bull; {p['base_min']:.0f} min/gm redistributed to teammates</div>
        <div class="card-scout">{_esc(p.get('inj_comment') or p.get('inj_status') or 'Out')}</div>
      </div></div>"""
    mu = p["mu"]
    tags = f'<span class="tag {"st" if p["starter_tonight"] else "bn"}">{"Starter" if p["starter_tonight"] else "Bench"}</span>'
    vs = "vs DK" if p["line_src"] == "DK" else "vs avg"
    if p["boost"] >= BOOST_CALL:
        tags += f'<span class="tag up" title="model is {p["boost"]:.1f} above the line">MORE {p["boost"]:+.1f} {vs}</span>'
    elif p["boost"] <= -BOOST_CALL:
        tags += f'<span class="tag down" title="model is {-p["boost"]:.1f} below the line">LESS {p["boost"]:+.1f} {vs}</span>'
    tags += _inj_tag(p)
    l10 = p.get("l10") or []
    l10_txt = " ".join(str(x) for x in l10[:10]) or "no games"
    ladder = "".join(f'<span title="P(PRA &ge; {x})">{x}+ {pr * 100:.0f}%</span>' for x, pr in p["ladder"])
    rest = (f"{p['rest_days']}d rest" if p.get("rest_days") is not None and p["rest_days"] <= 7
            else ("rested" if p.get("rest_days") is not None else "rest n/a"))
    if p.get("b2b"):
        rest = "back-to-back"
    ls = p.get("l10_stats") or {}
    md = p["min_delta"]
    md_clr = "#3fb950" if md >= 2 else ("#ff7b72" if md <= -2 else "#8b949e")
    spread = p.get("spread_t")
    spread_txt = f"{'+' if spread > 0 else ''}{spread:.1f}" if spread is not None else "n/a"
    proxy = p.get("proxy_line")
    if p["line_src"] == "DK":
        moved = (f" (opened {p['dk_open']:g})" if p.get("dk_open") is not None and p["dk_open"] != proxy else "")
        label_line = f"DK line {proxy:g} &bull; model {p['boost']:+.1f}"
        check_txt = f"DK line {proxy:g}{moved} &bull; type another line to check it"
        check_val = f"{proxy:g}"
        chance_sub = "leans on DK's line &mdash; the model gets a 10% say"
    else:
        label_line = f"boost {p['boost']:+.1f} vs L10"
        check_txt = (f"no DK line &bull; vs L10 line {proxy}: More {p['p_over_proxy'] * 100:.0f}%" if proxy is not None
                     else "no DK line")
        check_val = ""
        chance_sub = "no DK line &mdash; from the model alone"
    rot = 1 if p["min"] >= MIN_PROJ_MINUTES else 0
    return f"""
    <div class="player-card" id="p-{p['pid']}"{style} data-team="{p['team']}" data-main="{1 if p.get('main_slate', True) else 0}" data-pos="{p['grp']}" data-pra="{p['pra']:.2f}" data-pm="{p['p_mean']:.2f}" data-boost="{p['boost']:.2f}" data-var="{p['var']:.2f}" data-out="0" data-rot="{rot}">
      <div class="card-rank" style="background:{p['grade_color']}22;color:{p['grade_color']};border:1px solid {p['grade_color']}44">
        <span class="rk">#</span><strong style="font-size:1.2rem">{p['grade']}</strong>
      </div>
      <div class="card-body">
        <div class="card-name">
          <span class="nm" style="color:{team_color}">{_esc(p['name'])}</span>
          <span class="ctx">{_esc(p['pos'])} &bull; {p['team']} {home_away} {p['opp']} &bull; {p['start_str']} ET</span>
          {tags}
        </div>
        <div class="card-label" style="color:{p['grade_color']}">{p['pra']:.1f} PRA projected &bull; {mu['pts']:.1f} / {mu['reb']:.1f} / {mu['ast']:.1f} &bull; {label_line}</div>
        <div class="card-stats">
          <div><div class="stat-label">Chances</div>
            <div class="stat-val">{_bar(p['p_mean'], 55, '#f0883e')} {p['floor']}&ndash;{p['ceiling']} PRA (middle 50%)</div>
            <div class="stat-sub ladder">{ladder}</div>
            <div class="stat-sub">{chance_sub}</div></div>
          <div><div class="stat-label">Minutes</div>
            <div class="stat-val">{_bar(p['min'], 42, '#58a6ff')} {p['min']:.1f} proj <span style="color:{md_clr}">({md:+.1f})</span></div>
            <div class="stat-sub">last 5 {p['l5_min'] or 0:.1f} &bull; season {p['season_min'] or 0:.1f} &bull; started {p['st_rate'] * 100:.0f}% of last 5</div></div>
          <div><div class="stat-label">Points / threes</div>
            <div class="stat-val">{mu['pts']:.1f} pts &bull; {mu['fg3m']:.1f} 3PM</div>
            <div class="stat-sub">L10 {ls.get('pts', 0):.1f} / {ls.get('fg3m', 0):.1f} &bull; 20+ pts {p['p_pts20'] * 100:.0f}% &bull; 2+ 3PM {p['p_3pm2'] * 100:.0f}%</div></div>
          <div><div class="stat-label">Rebounds / assists</div>
            <div class="stat-val">{mu['reb']:.1f} reb &bull; {mu['ast']:.1f} ast</div>
            <div class="stat-sub">L10 {ls.get('reb', 0):.1f} / {ls.get('ast', 0):.1f} &bull; 10+ reb {p['p_reb10'] * 100:.0f}% &bull; 8+ ast {p['p_ast8'] * 100:.0f}%</div></div>
          <div><div class="stat-label">Environment</div>
            <div class="stat-val">{p['team']} implied {p['imp']:.1f} &bull; spread {spread_txt}</div>
            <div class="stat-sub">total {p['total'] if p.get('total') else 'n/a'} &bull; {'home' if p['is_home'] else 'road'} &bull; {rest} &bull; team shares {(p['scale']['pts'] - 1) * 100:+.0f}%</div></div>
          <div><div class="stat-label">Form (PRA, last 10)</div>
            <div class="stat-val">{p['l10_pra'] or 0:.1f} avg &bull; season {p['season_pra'] or 0:.1f}</div>
            <div class="stat-sub">{l10_txt}</div></div>
        </div>
        <div class="checker">PRA line <input type="number" step="0.5" inputmode="decimal" placeholder="e.g. 29.5" value="{check_val}" oninput="checkLine(this)"> <span class="res"></span> <span class="muted">{check_txt}</span></div>
        <div class="card-scout">{_esc(p['scout'])}</div>
      </div>
      <div class="card-score" style="color:{p['grade_color']}">{p['pra']:.0f}<small>PRA</small></div>
    </div>"""


def _edges_html(rows):
    """Where the model disagrees most with DraftKings' line, both ways."""
    live = [r for r in rows if not r.get("out") and r["min"] >= MIN_PROJ_MINUTES]
    pool = [r for r in live if r["line_src"] == "DK"]
    vs = "the DK line"
    if not pool:
        # Props not posted yet (early-morning run) -- fall back to the average line.
        pool = [r for r in live if r.get("proxy_line") is not None and len(r.get("l10") or []) >= 5]
        vs = "his recent average (no DK lines posted yet)"

    def table(lst, title, color):
        if not lst:
            return ""
        body = "".join(
            f'<tr onclick="jumpTo(\'{r["pid"]}\')"><td><b>{_esc(r["name"])}</b> <span class="muted">{r["team"]}</span></td>'
            f'<td class="r">{r["pra"]:.1f}</td><td class="r muted">{r["proxy_line"]:g}</td>'
            f'<td class="r" style="color:{color};font-weight:700">{r["boost"]:+.1f}</td>'
            f'<td class="r">{(r["p_over_proxy"] if r["boost"] > 0 else 1 - r["p_over_proxy"]) * 100:.0f}%</td></tr>'
            f'<tr onclick="jumpTo(\'{r["pid"]}\')"><td class="why" colspan="5">{_esc(r["scout"])}</td></tr>'
            for r in lst)
        line_hdr = "DK line" if vs == "the DK line" else "avg line"
        return (f'<div class="edge-box"><h3 style="color:{color}">{title}</h3><table>'
                f'<tr class="muted"><td style="font-size:11px">player</td><td class="r" style="font-size:11px">model</td>'
                f'<td class="r" style="font-size:11px">{line_hdr}</td><td class="r" style="font-size:11px">gap</td>'
                f'<td class="r" style="font-size:11px">chance</td></tr>{body}</table></div>')
    ups = sorted([r for r in pool if r["boost"] >= BOOST_CALL], key=lambda r: -r["boost"])[:8]
    downs = sorted([r for r in pool if r["boost"] <= -BOOST_CALL], key=lambda r: r["boost"])[:8]
    note = ('<p class="muted" style="font-size:12px;margin:0 0 10px">The model\'s biggest disagreements with '
            f'{vs}. Unproven: against last season\'s real DK lines (mostly playoffs) these went 49%, so the '
            '"chance" column leans on the line. The scorecard grades every one.</p>')
    if not ups and not downs:
        return note + '<div class="empty">The model is within 3 PRA of the line for every rotation player tonight.</div>'
    return (note + f'<div class="edges">{table(ups, "Model above the line", "#3fb950")}'
            f'{table(downs, "Model below the line", "#ff7b72")}</div>')


def _scorecard_html(history, total):
    if not history and not total.get("n"):
        return ('<div class="empty">No graded slates yet. Every board is logged at tip-off and scored the next '
                'morning against the box scores; the table fills in from day two.</div>')
    order = ["A+", "A", "B+", "B", "C", "D"]
    html = ""
    if total.get("n"):
        rows = ""
        for g in order:
            t = total["tiers"].get(g)
            if not t:
                continue
            gap = t["act"] - t["proj"]
            cls = "good" if abs(gap) < 1.5 else "bad"
            rows += (f'<tr><td><span style="color:{GRADE_COLORS[g]};font-weight:700">{g}</span></td>'
                     f'<td>{t["n"]}</td><td>{t["proj"]:.1f}</td><td class="{cls}">{t["act"]:.1f}</td>'
                     f'<td class="{cls}">{gap:+.1f}</td></tr>')
        mo, le = total["more"], total["less"]
        mo_txt = f"{mo['w'] / mo['n'] * 100:.0f}% of {mo['n']}" if mo["n"] else "none yet"
        le_txt = f"{le['w'] / le['n'] * 100:.0f}% of {le['n']}" if le["n"] else "none yet"
        vd = total.get("vs_dk") or {}
        dk_txt = (f"vs DraftKings' line ({vd['n']} players): model off by {vd['model'] / vd['n']:.1f}, DK off by "
                  f"{vd['dk'] / vd['n']:.1f}, went over {vd['over'] / vd['n'] * 100:.0f}% &bull; "
                  if vd.get("n") else "")
        html += f"""
        <p class="muted" style="font-size:13px;margin-bottom:8px">All-time: {total['days']} slates, {total['n']} rotation players &bull;
          PRA error {total['mae']:.1f}/player (bias {total['bias']:+.1f}) &bull; {dk_txt}
          model's More calls won {mo_txt} &bull; Less calls won {le_txt} (50% = coin flip; you need well above it)</p>
        <table class="score"><tr><th>Grade</th><th>Players</th><th>Projected PRA</th><th>Actual PRA</th><th>Gap</th></tr>{rows}</table>"""
    if history:
        rows = ""
        for r in history:
            flag = ' <span class="muted">(partial)</span>' if r.get("partial") else ""
            mo, le = r["more"], r["less"]
            mo_txt = f"{mo['w']}/{mo['n']}" if mo["n"] else "-"
            le_txt = f"{le['w']}/{le['n']}" if le["n"] else "-"
            pre = ' <span class="muted">(pre)</span>' if r.get("preseason") else ""
            vd = r.get("vs_dk") or {}
            dk_txt = f"{vd['model'] / vd['n']:.1f} / {vd['dk'] / vd['n']:.1f}" if vd.get("n") else "-"
            rows += (f'<tr><td>{r["date"]}{flag}{pre}</td><td>{r["n"]}</td><td>{r["mae"]:.1f}</td><td>{r["bias"]:+.1f}</td>'
                     f'<td>{r["min_mae"]:.1f}</td><td>{dk_txt}</td><td>{mo_txt}</td><td>{le_txt}</td>'
                     f'<td>{r["brier"] if r.get("brier") is not None else "-"}</td></tr>')
        html += f"""
        <div class="scroll"><table class="score"><tr><th>Slate</th><th>Players</th><th>PRA err</th><th>Bias</th><th>Min err</th><th>Model / DK err</th><th>More won</th><th>Less won</th><th>Brier</th></tr>{rows}</table></div>"""
    return html


def render_html(slate_date, games, lines, rows, timestamp, history, total,
                preseason=False, backtest=False, lock_str=""):
    date_str = slate_date.strftime("%A, %B %d, %Y")
    banner = ""
    if backtest:
        banner = (f'<div class="banner">BACKTEST -- {date_str} rebuilt from what was known that morning '
                  f'(only earlier box scores, closing DraftKings lines, injury DNPs as the OUT list). '
                  f'Scored below against the real box scores.</div>')
    elif preseason:
        banner = ('<div class="banner">PRESEASON DRY RUN -- exhibition slate. Starters play 20 minutes and '
                  'coaches experiment, so treat the numbers as the pipeline warming up; the real board starts on '
                  'opening night.</div>')
    elif not games:
        banner = '<div class="banner">No NBA games today. The scorecard is below.</div>'

    chips = ('<div class="game-chip active all-chip" id="all-chip" onclick="pickGame(this)"><div class="teams">All games</div>'
             '<div class="time">tap a game to filter</div></div>')
    for g in games:
        ln = lines.get(g["game_id"]) or {}
        sp = ln.get("spread")
        fav = (f"{g['home']} {sp:+.1f}" if sp is not None and sp <= 0 else
               (f"{g['away']} {-sp:+.1f}" if sp is not None else "no line"))
        tot = f"O/U {ln['total']:.1f}" if ln.get("total") else "no total"
        outs = [r["name"].split()[-1] for r in rows if r.get("out") and r["team"] in (g["home"], g["away"])
                and r.get("recent_team_presence", 0) >= 3 and r.get("base_min", 0) >= 15]
        state = " &bull; FINAL" if g.get("final") else (" &bull; LIVE" if g.get("started") else "")
        chips += f"""<div class="game-chip" data-teams="{g['away']},{g['home']}" data-main="{1 if g['main_slate'] else 0}" onclick="pickGame(this)">
          <div class="teams">{_logo(g['away'])}{g['away']} @ {_logo(g['home'])}{g['home']}</div>
          <div class="time">{g['start_str']} ET{state} &bull; {tot} &bull; {fav}</div>
          <div class="meta">{('OUT: ' + _esc(', '.join(outs[:4]))) if outs else 'no rotation players out'}</div></div>"""

    ranked = sorted(rows, key=lambda r: (bool(r.get("out")), -r["pra"]))
    cards = "".join(_player_card(p) for p in ranked)
    board = cards if rows else '<div class="empty">No players on the slate.</div>'
    scorecard = _scorecard_html(history, total)
    n_games = len(games)
    priced = sum(1 for g in games if (lines.get(g["game_id"]) or {}).get("total"))
    n_dk = sum(1 for r in rows if not r.get("out") and r.get("line_src") == "DK")
    n_out = sum(1 for r in rows if r.get("out") and r.get("base_min", 0) >= 15 and r.get("recent_team_presence", 0) >= 3)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Basketball Guru - NBA {slate_date.isoformat()}</title>
<link rel="manifest" href="manifest.json"><link rel="apple-touch-icon" href="icon.png"><link rel="icon" href="icon.png">
<meta name="theme-color" content="#0d1117">
<style>{CSS}</style>
</head><body>
<div id="splash"><div class="ball"></div><div class="splash-title">BASKETBALL GURU</div><div class="splash-sub">who fills the sheet tonight</div></div>
<div class="header">
  <h1>BASKETBALL GURU</h1>
  <p>NBA PRA board &bull; {date_str} &bull; {n_games} games</p>
  <p>Generated {timestamp}{(' &bull; <span class="lock">first tip ' + lock_str + ' ET</span>') if lock_str else ''}</p>
  <p style="font-size:12px;color:#6e7681">DK game lines {priced}/{n_games} &bull; DK PRA lines for {n_dk} players &bull; {n_out} rotation players OUT &bull; grades: {' '.join(f'<span style="color:{GRADE_COLORS[g]}">{g} &ge;{c:.0f}</span>' for g, c in PRA_GRADES)}</p>
  {banner}
</div>
<div class="container">
  <div class="about" id="about">
    <button class="about-bar" onclick="toggleAbout()"><span><b style="color:#ffa657">How this works</b> -- what the numbers mean and where they come from</span><span>&#9662;</span></button>
    <div class="about-body">
      <p><b>The headline is projected Points + Rebounds + Assists</b> for the night he plays (a prop on a player who sits is voided). It is built bottom-up: projected minutes &times; what he produces per minute (both seasons, recent games weighted up). Then every team is made to add up: minutes to 240 plus a little overtime, and points to what DraftKings' spread and total say the team will score. When a starter is ruled out, his minutes and his share of the shots go to the players left -- the backups at his position first. The opponent counts through those team totals (pace, points, rebounds and assists it allows); there is no defense-vs-position adjustment, because in the 2025-26 backtest it added nothing.</p>
      <p><b>DK line</b> is DraftKings' actual PRA line, pulled free from ESPN for every player DK prices. The number next to it is the model's disagreement: projection minus line. MORE and LESS tags mark disagreements of 3+.</p>
      <p><b>Be careful with the tags.</b> Tested against 1,179 real DK lines from last season (mostly playoffs), DraftKings was more accurate than the model (off by 6.2 a player vs 6.5), and when the model disagreed by 3+ it was right only 49% of the time. So the percentages on this board lean on DK's line -- the model gets a 10% say -- and the tags are the model's opinion, graded every night on the scorecard. Trust them once the scorecard shows them winning well above 50%, not before. Players DK does not price fall back to their recent average as the line (marked "vs avg").</p>
      <p><b>Line checker:</b> each card is filled in with DK's line and shows the More / Less percentage. To check a different line, type it in. The ladder and the range use the same numbers.</p>
      <p><b>The scorecard</b> at the bottom is the honesty check: every board is logged at tip-off and graded the next morning against the box scores -- PRA error, whether the grades sort players, the model vs DK's own line on the same players, and whether the MORE / LESS calls beat a coin flip.</p>
      <p style="color:#8b949e">Data: ESPN (schedule, box scores, rosters, injuries, DraftKings spreads, totals and PRA lines). No paid feeds.</p>
    </div>
  </div>
  <div class="toolbar">
    <button class="toggle sort-btn on" onclick="setSort(this,'pra')">Rank: PRA</button>
    <button class="toggle sort-btn" onclick="setSort(this,'boost')">Model above line</button>
    <button class="toggle sort-btn" onclick="setSort(this,'fade')">Model below line</button>
    <span class="sep"></span>
    <button class="toggle pos-btn on" onclick="setPos(this,'ALL')">All</button>
    <button class="toggle pos-btn" onclick="setPos(this,'G')">G</button>
    <button class="toggle pos-btn" onclick="setPos(this,'F')">F</button>
    <button class="toggle pos-btn" onclick="setPos(this,'C')">C</button>
    <span class="sep"></span>
    <button class="toggle" onclick="toggleMain(this)">Main slate (7 PM+)</button>
  </div>
  <div class="games-row">{chips}</div>
  <div class="filter-banner" id="filter-banner"></div>
  <h2 class="section-title">Model vs DraftKings <small>biggest disagreements; tap a name for the card</small></h2>
  {_edges_html(rows)}
  <h2 class="section-title">The board <small>projected PRA if he plays; each card shows DK's line and the More / Less odds</small></h2>
  <div id="board">{board}</div>
  <button class="show-all" id="show-all" onclick="showAll(this)">Show all</button>
  <h2 class="section-title">Scorecard <small>logged at tip-off, graded the next morning</small></h2>
  {scorecard}
</div>
<div class="footer">Basketball Guru &bull; paper picks only &bull; projections are not betting advice</div>
<script>var TOP_SHOWN = {TOP_SHOWN};{JS}
applyFilters();
document.querySelectorAll('.checker input').forEach(function (i) {{ if (i.value) checkLine(i); }});</script>
</body></html>"""


# ============================================================
#  ICON + MANIFEST GENERATION
# ============================================================

def create_ball_png(size=180):
    """180x180 PNG of a basketball on the board's dark background -- no external libs needed."""
    import struct, zlib
    w = h = size
    cx, cy, rad = w / 2, h / 2, w * 0.40
    BG, BALL, SHADE, SEAM = (13, 17, 23, 255), (240, 136, 62, 255), (194, 65, 12, 255), (40, 18, 6, 255)
    seam = max(2.0, w * 0.022)
    rows = []
    for y in range(h):
        row = bytearray(b'\x00')
        for x in range(w):
            dx, dy = x - cx, y - cy
            dist = math.hypot(dx, dy)
            if dist > rad:
                c = BG
            else:
                # The two straight seams plus two curved ones, like a real ball from the side.
                on = abs(dx) < seam / 2 or abs(dy) < seam / 2
                for side in (-1, 1):
                    ccx = cx + side * rad * 1.35
                    if abs(math.hypot(x - ccx, dy) - rad * 1.0) < seam / 2:
                        on = True
                if on or dist > rad - seam:
                    c = SEAM
                else:
                    t = clip((dx + dy) / (2 * rad) + 0.35, 0, 1)
                    c = tuple(int(BALL[i] * (1 - t) + SHADE[i] * t) for i in range(3)) + (255,)
            row += bytes(c)
        rows.append(bytes(row))
    raw = b''.join(rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)

    png = b'\x89PNG\r\n\x1a\n'
    png += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', zlib.compress(raw, 9))
    png += chunk(b'IEND', b'')
    return png


def create_manifest():
    """Web app manifest so Android Chrome installs it as a standalone app."""
    return json.dumps({
        "name": "Basketball Guru", "short_name": "BasketballGuru",
        "description": "NBA Nightly PRA Board",
        "start_url": "./", "display": "standalone",
        "background_color": "#0d1117", "theme_color": "#f0883e",
        "orientation": "any",
        "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png", "purpose": "any maskable"}],
    }, indent=2)


# ============================================================
#  GITHUB DEPLOY
# ============================================================

def deploy_file(content_bytes, filename, headers, commit_msg):
    """Push a single file (bytes) to the repo through the Contents API."""
    import base64
    url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    try:
        sha = requests.get(url, headers=headers, timeout=20).json().get("sha")
    except Exception:
        sha = None
    body = {"message": commit_msg, "content": base64.b64encode(content_bytes).decode(), "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=headers, json=body, timeout=30)
    return r.status_code in (200, 201)


def deploy(html):
    if not GITHUB_TOKEN:
        print("  [!] No GITHUB_TOKEN -- skipping deploy")
        return False
    headers = _gh_headers()
    msg = f"Basketball Guru update {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    deploy_file(create_ball_png(), "icon.png", headers, msg)
    deploy_file(create_manifest().encode(), "manifest.json", headers, msg)
    ok = deploy_file(html.encode(), "index.html", headers, msg)
    print(f"  [OK] Deployed --> {PAGES_URL}" if ok else "  [!] Deploy failed")
    return ok


# ============================================================
#  BACKTEST / CALIBRATION
# ============================================================

def load_db(d, verbose=True):
    """
    Box scores of the season d belongs to (before d), the one before it, and
    this season's preseason -- the last only ever feeds players with no NBA
    games at all (every other query filters to regular season + playoffs).
    """
    season = season_of(d)
    boxes = (load_boxes(season - 1, date(season - 1, 7, 31), verbose) + load_boxes(season, d, verbose)
             + load_boxes(season, d, verbose, preseason=True))
    return DB(boxes), season


def slate_from_boxes(boxes):
    """
    A past slate rebuilt from its own box scores: who dressed, who was out
    injured (known before tip), the closing DraftKings line. Coach's-decision
    DNPs stay in the pool -- nobody knew those in advance.
    """
    games, rosters, out_ids, lines, actual = [], {}, set(), {}, {}
    for b in boxes:
        games.append({"game_id": b["id"], "home": b["home"], "away": b["away"], "type": b["type"],
                      "start_str": "", "main_slate": True, "started": True, "final": True,
                      "home_name": TEAMS[b["home"]][1], "away_name": TEAMS[b["away"]][1],
                      "start_et": None, "state": "STATUS_FINAL",
                      "home_score": b["hs"], "away_score": b["as"]})
        lines[b["id"]] = b.get("odds")
        for r in b["p"]:
            rosters.setdefault(r[C["team"]], []).append(r[0])
            actual[r[0]] = {"min": r[C["min"]], **{k: r[C[k]] for k in STATS},
                            "pra": r[C["pts"]] + r[C["reb"]] + r[C["ast"]], "team": r[C["team"]]}
        for pid, team, reason in b["dnp"]:
            rosters.setdefault(team, []).append(pid)
            if "COACH" not in reason:
                out_ids.add(pid)
    return games, rosters, out_ids, lines, actual


def boxes_on(d, season):
    sched = season_schedule(season)
    out = []
    for gid, gd, done in sched:
        if gd == d.isoformat():
            b = get_box(gid, season)
            if b:
                out.append(b)
    return out


def calibrate(start, end):
    """
    Replay every slate in [start, end] with only what was known that morning
    and compare to what happened. The test that matters: does the model beat
    simply quoting each player's last-10 average (which is what a lazy line
    is)?
    """
    season = season_of(start)
    db, _ = load_db(end + timedelta(days=1))
    recs = []
    d = start
    while d <= end:
        boxes = [b for b in boxes_on(d, season) if b["type"] in (2, 3)]
        if boxes:
            games, rosters, out_ids, lines, actual = slate_from_boxes(boxes)
            dk = get_dk_lines(games, season, live=False)
            rows, _, _ = project_slate(db, games, d.isoformat(), season, rosters, out_ids, lines, dk=dk)
            for r in rows:
                if r.get("out") or r["pid"] not in actual:
                    continue
                a = actual[r["pid"]]
                if a["min"] <= 0:
                    continue
                recs.append({"d": d.isoformat(), "mu": r["mu"], "pra": r["pra"], "min": r["min"],
                             "a": a, "l10": r["ref_pra"], "boost": r["boost"], "st": r["starter_tonight"],
                             "spread": abs(r["spread_t"] or 0), "b2b": r["b2b"], "grade": r["grade"],
                             "p_over": r["p_over_proxy"], "proxy": r["proxy_line"], "gp": r["gp_cur"],
                             "src": r["line_src"],
                             "mates_out": len(r["mates_out"]), "base_min": r["base_min"]})
        d += timedelta(days=1)
    return recs


def calibration_report(recs, min_proj=15.0):
    import statistics as stx
    R = [r for r in recs if r["min"] >= min_proj and r["l10"] is not None]
    print(f"\n  {len(recs)} player-games, {len(R)} with >= {min_proj:.0f} projected minutes and a last-10\n")
    print(f"  {'stat':<6} {'model MAE':>9} {'L10 MAE':>8} {'bias':>7}")
    for k in ("pts", "reb", "ast", "fg3m"):
        mae = _mean(abs(r["a"][k] - r["mu"][k]) for r in R)
        bias = _mean(r["a"][k] - r["mu"][k] for r in R)
        print(f"  {k:<6} {mae:9.2f} {'':>8} {bias:+7.2f}")
    mae = _mean(abs(r["a"]["pra"] - r["pra"]) for r in R)
    l10 = _mean(abs(r["a"]["pra"] - r["l10"]) for r in R)
    bias = _mean(r["a"]["pra"] - r["pra"] for r in R)
    print(f"  {'PRA':<6} {mae:9.2f} {l10:8.2f} {bias:+7.2f}")
    print(f"  {'MIN':<6} {_mean(abs(r['a']['min'] - r['min']) for r in R):9.2f} {'':>8} "
          f"{_mean(r['a']['min'] - r['min'] for r in R):+7.2f}")

    print("\n  By projected PRA (mean proj / mean actual / residual SD / model SD):")
    for lo, hi in ((0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 99)):
        b = [r for r in R if lo <= r["pra"] < hi]
        if len(b) < 30:
            continue
        res = [r["a"]["pra"] - r["pra"] for r in b]
        mu = _mean(r["pra"] for r in b)
        print(f"    {lo:>2}-{hi:<2} n={len(b):<5} {mu:5.1f} / {_mean(r['a']['pra'] for r in b):5.1f}"
              f"   resid SD {stx.pstdev(res):5.2f}   model SD {math.sqrt(stat_var('pra', mu)):5.2f}")

    # Against DraftKings' real line, where archived lines exist. DK_MODEL_W
    # is re-fitted here: the model's share of the say that minimises the miss.
    D = [r for r in recs if r.get("src") == "DK"]
    if len(D) >= 100:
        print(f"\n  Against DraftKings' real line ({len(D)} player-games):")
        print(f"    miss vs actual: model {_mean(abs(r['a']['pra'] - r['pra']) for r in D):.2f}   "
              f"DK line {_mean(abs(r['a']['pra'] - r['proxy']) for r in D):.2f}   "
              f"went over {_mean(1.0 if r['a']['pra'] > r['proxy'] else 0.0 for r in D) * 100:.1f}%")
        for t in (2, 3, 4):
            c = [r for r in D if abs(r["pra"] - r["proxy"]) >= t]
            w = sum(1 for r in c if (r["a"]["pra"] > r["proxy"]) == (r["pra"] > r["proxy"]))
            print(f"    model {t}+ away from the line: right {w}/{len(c)} = {w / len(c) * 100 if c else 0:.1f}%")
        print("    model's share of the say (DK_MODEL_W) -> miss:", "  ".join(
            f"{w:.1f}:{_mean(abs(r['a']['pra'] - (r['proxy'] + w * (r['pra'] - r['proxy']))) for r in D):.3f}"
            for w in (0.0, 0.1, 0.2, 0.3, 0.5)))
    R = [r for r in R if r.get("src") != "DK"]

    print("\n  Boost (proj - L10) vs what happened relative to L10:")
    for lo, hi in ((-99, -4), (-4, -2), (-2, 0), (0, 2), (2, 4), (4, 99)):
        b = [r for r in R if lo <= r["boost"] < hi]
        if len(b) < 30:
            continue
        over = _mean(1.0 if r["a"]["pra"] > r["proxy"] else 0.0 for r in b)
        p = _mean(r["p_over"] for r in b)
        print(f"    boost {lo:>3}..{hi:<3} n={len(b):<5} mean boost {_mean(r['boost'] for r in b):+5.1f}  "
              f"actual-L10 {_mean(r['a']['pra'] - r['l10'] for r in b):+5.1f}  "
              f"over L10 line {over * 100:4.1f}% (model said {p * 100:4.1f}%)")

    print("\n  Starter minutes vs |spread| (actual - projected):")
    for lo, hi in ((0, 4), (4, 8), (8, 12), (12, 99)):
        b = [r for r in R if r["st"] and lo <= r["spread"] < hi]
        if len(b) >= 30:
            print(f"    |spread| {lo:>2}-{hi:<2} n={len(b):<5} {_mean(r['a']['min'] - r['min'] for r in b):+5.2f} min")
    b = [r for r in R if r["st"] and r["b2b"]]
    if b:
        print(f"    back-to-back starters n={len(b)}  {_mean(r['a']['min'] - r['min'] for r in b):+5.2f} min")
    for lab, f in (("teammates out", lambda r: r["mates_out"] > 0), ("full team", lambda r: r["mates_out"] == 0)):
        b = [r for r in R if f(r)]
        if b:
            print(f"    {lab:<14} n={len(b):<5} PRA bias {_mean(r['a']['pra'] - r['pra'] for r in b):+5.2f}  "
                  f"MAE {_mean(abs(r['a']['pra'] - r['pra']) for r in b):5.2f} vs L10 {_mean(abs(r['a']['pra'] - r['l10']) for r in b):5.2f}")
    print("\n  By grade (proj / actual):")
    for g in ("A+", "A", "B+", "B", "C", "D"):
        b = [r for r in R if r["grade"] == g]
        if b:
            print(f"    {g:<3} n={len(b):<5} {_mean(r['pra'] for r in b):5.1f} / {_mean(r['a']['pra'] for r in b):5.1f}")
    # Brier on the proxy line, model vs a coin flip
    br = _mean((r["p_over"] - (1.0 if r["a"]["pra"] > r["proxy"] else 0.0)) ** 2 for r in R)
    print(f"\n  Brier on the L10 line: {br:.4f} (0.25 = coin flip)")

    # Spread model: fit Var = a * mu^c per stat on binned residuals (all
    # projected players, not just rotation, so the low end is covered too).
    print("\n  Variance fit (Var = a * mu^c):")
    allr = [r for r in recs if r["min"] >= 8]
    for k in ("pts", "reb", "ast", "fg3m", "pra"):
        get_mu = (lambda r: r["pra"]) if k == "pra" else (lambda r, k=k: r["mu"][k])
        srt = sorted(allr, key=get_mu)
        pts = []
        step = max(200, len(srt) // 12)
        for i in range(0, len(srt) - step + 1, step):
            b = srt[i:i + step]
            mu = _mean(get_mu(r) for r in b)
            v = _mean((r["a"][k] - get_mu(r)) ** 2 for r in b)
            if mu > 0.05 and v > 0:
                pts.append((math.log(mu), math.log(v)))
        if len(pts) >= 3:
            mx, my = _mean(x for x, _ in pts), _mean(y for _, y in pts)
            c = sum((x - mx) * (y - my) for x, y in pts) / sum((x - mx) ** 2 for x, _ in pts)
            a = math.exp(my - c * mx)
            print(f"    {k:<5} a={a:6.3f} c={c:5.3f}   (now {VAR_MODEL[k]})")

    # Would leaning toward the last-10 average help? (> 0 means the model
    # over-reacts to tonight's circumstances.)
    print("\n  Shrink toward L10 (PRA MAE):", "  ".join(
        f"{lam:.2f}:{_mean(abs(r['a']['pra'] - (r['pra'] + lam * (r['l10'] - r['pra']))) for r in R):.3f}"
        for lam in (0.0, 0.1, 0.2, 0.3)))


# ============================================================
#  MAIN
# ============================================================

# The moments the board NEEDS to be fresh, Eastern time. These are NOT the
# cron times -- GitHub's free cron runs 1-3 h late, so the yml fires several
# crons ahead of each window and the skip/quiet logic sorts out the pile-up.
#   8:00 AM  score last night, warm the caches
#   1:00 PM  shootarounds done, the 1:30 injury report is close
#   5:30 PM  pre-lock board (the main slate locks at the 7 PM tip)
#   6:40 PM  last look: late scratches, starting lineups
SCHEDULE_ET = [(8, 0), (13, 0), (17, 30), (18, 40)]
SKIP_IF_FRESHER_THAN_MIN = 20    # a scheduled run exits if the board is this fresh
# A quiet refresh (nobody newly OUT, board recently pushed) earns no phone
# ping -- otherwise dense crons mean six identical pushes.
NOTIFY_QUIET_REFRESH_MIN = 120
RUN_CHECK_GRACE_MIN      = 55    # GitHub cron runs 30-60 min late; check after that
NTFY_RUN_CHECKS          = True
RUN_CHECK_PRIORITY       = "default"


def is_scheduled_run():
    return os.environ.get("GITHUB_EVENT_NAME", "") == "schedule"


def live_board_age_minutes():
    """Minutes since index.html was last committed, or None if unknown."""
    if not GITHUB_TOKEN:
        return None
    try:
        from datetime import timezone
        c = requests.get(f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/commits",
                         params={"path": "index.html", "per_page": 1}, headers=_gh_headers(),
                         timeout=15).json()
        last = datetime.strptime(c[0]["commit"]["committer"]["date"],
                                 "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() / 60
    except Exception as e:
        print(f"  board-age check failed ({e}) -- running anyway")
        return None


def next_scheduled_slot(now):
    best = None
    for hh, mm in SCHEDULE_ET:
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best:
            best = cand
    return best


def book_run_check(now):
    """
    Schedule a delayed ntfy message for shortly after the next expected run.
    If GitHub's cron never fires, nothing runs, so nothing can alert you --
    this is the only piece that works from outside GitHub. Worded as a check:
    if an "updated" push already came, ignore it.
    """
    if not (NTFY_ENABLED and NTFY_RUN_CHECKS):
        return
    slot = next_scheduled_slot(now)
    fire = slot + timedelta(minutes=RUN_CHECK_GRACE_MIN)
    state = cache_load("run_check_state", permanent=True) or {}
    if state.get("booked") == slot.isoformat():
        print(f"  Run-check for {slot.strftime('%a %I:%M %p')} already booked")
        return
    try:
        headers = {
            "Title": f"Fresh after {slot.strftime('%a %I:%M %p')}?".encode("utf-8"),
            "Priority": RUN_CHECK_PRIORITY, "Tags": "hourglass",
            "Delay": str(int(fire.timestamp())),
            "Actions": f"view, Run workflow, {ACTIONS_URL}; view, Open board, {PAGES_URL}",
        }
        body = (f"The board should have refreshed after {slot.strftime('%a %I:%M %p ET')}. "
                f"If you got an 'updated' push since then, ignore this. "
                f"If not, GitHub is behind -- tap Run workflow.")
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=15)
        print(f"  Booked run-check for {fire.strftime('%a %I:%M %p ET')}")
        cache_save("run_check_state", {"booked": slot.isoformat()}, permanent=True)
    except Exception as e:
        print(f"  [!] Could not book run-check: {e}")


def run(args):
    global NTFY_ENABLED
    if args.no_notify:
        NTFY_ENABLED = False
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    # Backup crons fire 30 min apart. If the primary already deployed, exit
    # quietly -- no double work, no double push. Manual runs always run.
    if is_scheduled_run():
        age = live_board_age_minutes()
        if age is not None and age < SKIP_IF_FRESHER_THAN_MIN:
            print(f"Scheduled run: board deployed {age:.0f} min ago -- skipping (backup trigger).")
            return
        if age is not None:
            print(f"Scheduled run: board is {age:.0f} min old -- running.")

    now = now_et()
    timestamp  = now.strftime("%Y-%m-%d %I:%M %p %Z")
    slate_date = date.fromisoformat(args.date) if args.date else now.date()
    backtest   = bool(args.backtest)
    do_deploy  = not (args.no_deploy or backtest)
    season     = season_of(slate_date)

    print(f"\n[BASKETBALL GURU] NBA {slate_date}  --  {timestamp}{'  (BACKTEST)' if backtest else ''}\n")

    print("  Fetching the slate...")
    if backtest:
        boxes = [b for b in boxes_on(slate_date, season) if b["type"] in (2, 3)]
        games, rosters, out_ids, lines, _ = slate_from_boxes(boxes)
        preseason = False
        injuries = {}
        for b in boxes:
            for pid, team, reason in b["dnp"]:
                if "COACH" not in reason:
                    injuries[pid] = {"status": "Out", "detail": reason.title(), "comment": reason.title(),
                                     "out": True, "note": "", "return": "", "name": ""}
    else:
        games, preseason = get_slate(slate_date)
        lines = {g["game_id"]: g["odds_raw"] for g in games}
    print(f"  {len(games)} games{' (PRESEASON dry run)' if preseason else ''}")
    for g in games:
        ln = lines.get(g["game_id"]) or {}
        print(f"    {g['away']} @ {g['home']}  {g['start_str']} ET  "
              f"[{'total ' + str(ln.get('total')) if ln.get('total') else 'no line'}"
              f"{', spread ' + str(ln.get('spread')) if ln.get('spread') is not None else ''}]")
    print()

    slate_teams = sorted({t for g in games for t in (g["home"], g["away"])})
    if not backtest and games:
        print("  Fetching injuries and rosters...")
        injuries = get_injuries(slate_date)
        roster_info = get_rosters(slate_teams)
        rosters = {t: [p["pid"] for p in lst] for t, lst in roster_info.items()}
        out_ids = {pid for pid, v in injuries.items() if v["out"]}
        print(f"  {len(out_ids)} players out league-wide, "
              f"{sum(1 for v in injuries.values() if not v['out'])} day-to-day\n")
    elif not backtest:
        injuries, rosters, out_ids = {}, {}, set()

    print("  Fetching DraftKings PRA lines...")
    dk = get_dk_lines(games, season, live=not backtest) if games else {}
    print(f"  {sum(len(v) for v in dk.values())} player lines across {sum(1 for v in dk.values() if v)}/{len(games)} games\n")

    print("  Loading box scores...")
    db, _ = load_db(slate_date)
    print(f"  {sum(len(v) for v in db.tlog.values()) // 2} games in the database, {len(db.plog)} players\n")

    print("  Projecting...")
    rows, tprof, lg = project_slate(db, games, slate_date.isoformat(), season, rosters, out_ids, lines, injuries, dk)
    live = [r for r in rows if not r.get("out")]
    ranked = sorted(live, key=lambda r: -r["pra"])
    for r in ranked[:12]:
        print(f"    {r['grade']:<3} {r['name']:<26} {r['team']:<4} {'vs' if r['is_home'] else '@ '} {r['opp']:<4} "
              f"{r['min']:4.1f}m  {r['pra']:5.1f} PRA ({r['mu']['pts']:.1f}/{r['mu']['reb']:.1f}/{r['mu']['ast']:.1f})  "
              f"{'DK ' + format(r['proxy_line'], 'g') if r['line_src'] == 'DK' else 'no DK line'} ({r['boost']:+.1f})")
    print()

    print("  Logging predictions for later scoring...")
    if not backtest:
        sync_predictions_down(slate_date)
    prev_out  = logged_out(slate_date) if not backtest else None
    board_age = live_board_age_minutes() if do_deploy else None
    pred_path = log_predictions(slate_date, rows, games, preseason, backtest) if games else None
    if pred_path and not backtest:
        sync_predictions_up(pred_path)
    rot_out = {r["name"] for r in rows if r.get("out") and r.get("base_min", 0) >= 15
               and r.get("recent_team_presence", 0) >= 3}
    newly_out = sorted(rot_out - prev_out) if prev_out is not None else []

    if backtest:
        result = score_date(slate_date, backtest=True, force=True)
        history, total = ([result] if result else []), {}
        if result:
            print(f"  Backtest: {result['n']} players, PRA error {result['mae']} (bias {result['bias']:+}), "
                  f"minutes error {result['min_mae']}")
            for k in ("more", "less"):
                v = result[k]
                if v["n"]:
                    print(f"    {k.title()} calls: {v['w']}/{v['n']} won")
            for g in ("A+", "A", "B+", "B", "C", "D"):
                t = result["tiers"].get(g)
                if t:
                    print(f"    {g:<3} n={t['n']:<4} proj {t['proj']:5.1f}  actual {t['act']:5.1f}")
    else:
        history, total = accuracy_history(slate_date)
        if history:
            print(f"  Scored {len(history)} prior slate(s) against box scores")

    print("  Rendering dashboard...")
    lock_str = games[0]["start_str"] if games else ""
    html = render_html(slate_date, games, lines, rows, timestamp, history, total,
                       preseason=preseason, backtest=backtest, lock_str=lock_str)
    fname = f"nba_{slate_date.isoformat()}{'_backtest' if backtest else ''}.html"
    fpath = os.path.join(OUTPUT_FOLDER, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [OK] Saved: {fpath}")
    if AUTO_OPEN_BROWSER and not args.no_open:
        try:
            webbrowser.open("file://" + os.path.abspath(fpath))
        except Exception:
            pass

    if not do_deploy:
        print("  Deploy skipped.")
        return

    deployed = deploy(html)
    if deployed and verify_live(timestamp):
        quiet = (board_age is not None and board_age < NOTIFY_QUIET_REFRESH_MIN
                 and not newly_out and is_scheduled_run())
        if NTFY_NOTIFY_SUCCESS and not quiet:
            msg = []
            if ranked:
                t = ranked[0]
                msg.append(f"Top PRA: {t['name']} ({t['team']} {'vs' if t['is_home'] else '@'} {t['opp']}) "
                           f"{t['pra']:.1f} {t['grade']}")
            gaps = sorted((r for r in live if r["min"] >= MIN_PROJ_MINUTES and r["line_src"] == "DK"
                           and abs(r["boost"]) >= BOOST_CALL), key=lambda r: -abs(r["boost"]))[:3]
            if gaps:
                msg.append("Model vs DK: " + "; ".join(f"{r['name']} {r['pra']:.1f} vs {r['proxy_line']:g}" for r in gaps))
            if newly_out:
                msg.append("Newly OUT: " + ", ".join(newly_out[:6]))
            if history and history[0]["date"] == (slate_date - timedelta(days=1)).isoformat():
                y = history[0]
                mo = y["more"]
                vd = y.get("vs_dk") or {}
                msg.append(f"Last night: PRA error {y['mae']:.1f}"
                           + (f" (DK line {vd['dk'] / vd['n']:.1f})" if vd.get("n") else "")
                           + (f", More {mo['w']}/{mo['n']}" if mo["n"] else "")
                           + (f", Less {y['less']['w']}/{y['less']['n']}" if y["less"]["n"] else ""))
            title = (f"Basketball Guru: {len(games)} games{' (preseason)' if preseason else ''}"
                     if games else "Basketball Guru: no games today")
            notify(title, "\n".join(msg) or "Board updated.", tags="basketball", click=PAGES_URL)
        elif quiet:
            print("  Quiet refresh -- no push")
    elif deployed:
        notify("Basketball Guru: Pages stale", "Push succeeded but the live site did not update. Tap Run workflow.",
               tags="warning", priority="high", click=PAGES_URL)
    if is_scheduled_run() or os.environ.get("GITHUB_ACTIONS") == "true":
        book_run_check(now_et())


def parse_args():
    import argparse
    ap = argparse.ArgumentParser(description="Basketball Guru -- NBA nightly PRA board")
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: today, Eastern)")
    ap.add_argument("--backtest", action="store_true",
                    help="grade the rebuilt slate against its box scores immediately (past dates)")
    ap.add_argument("--calibrate", nargs=2, metavar=("START", "END"),
                    help="run the engine over every slate in a date range and report calibration")
    ap.add_argument("--backfill", type=int, metavar="SEASON",
                    help="pull every box score of an ESPN season (e.g. 2026 = 2025-26)")
    ap.add_argument("--no-deploy", action="store_true", help="build locally only")
    ap.add_argument("--no-notify", action="store_true", help="no ntfy pushes")
    ap.add_argument("--no-open", action="store_true", help="do not open the board in a browser")
    return ap.parse_args()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")   # Actions logs show progress live
    except Exception:
        pass
    a = parse_args()
    if a.backfill:
        t0 = time.time()
        bx = load_boxes(a.backfill, date(a.backfill, 7, 31))
        print(f"{len(bx)} boxes in {time.time() - t0:.0f}s")
    elif a.calibrate:
        t0 = time.time()
        recs = calibrate(date.fromisoformat(a.calibrate[0]), date.fromisoformat(a.calibrate[1]))
        calibration_report(recs)
        print(f"\n  ({time.time() - t0:.0f}s)")
    else:
        try:
            run(a)
        except SystemExit:
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            notify("Basketball Guru FAILED", f"{type(e).__name__}: {e}", tags="rotating_light", priority="high")
            sys.exit(1)
