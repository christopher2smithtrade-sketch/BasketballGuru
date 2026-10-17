# CLAUDE.md — Basketball Guru

## Purpose

Nightly NBA PRA (Points + Rebounds + Assists) board for DraftKings PRA props. For
every player on the slate it projects PRA and the split (PTS / REB / AST / 3PM),
DraftKings' actual PRA line (free from ESPN), the model's disagreement with it,
and More / Less odds that lean on that line.
Same playbook as Hockey Guru (single file, Actions cron, Pages board, ntfy,
prediction log + morning scorecard) — basketball-specific engine underneath.

## Scripts

| File | Purpose |
|------|---------|
| `BasketballGuru.py` | The whole thing: data pull, projection engine, HTML board, prediction log + scorecard, calibration, GitHub Pages deploy, ntfy |
| `.github/workflows/basketball.yml` | Cron schedule (10 runs/day across four ET windows) |
| `predictions/pred_*.json` | One per slate, logged at tip-off, graded next morning (synced to the repo) |
| `cache/season/box_<season>/` | One compact box score per finished game (~4 KB) — committed once, never changes. ESPN season = the year it ends (2026 = 2025-26) |
| `cache/season/schedule_<season>.json` / `preseason_<season>.json` | Finished seasons' game lists, regular season and preseason (committed). Preseason box scores share the `box_<season>/` folder; every query except the rookie fallback filters them out by game type |
| `cache/season/props_<season>.json` | Archive of DraftKings' PRA line per game per player, the last one seen before tip-off (committed). ESPN drops props after a while, so this is the only history we keep |
| `cache/daily/` | Rosters, current schedule, run-check state — git-ignored, carried by `actions/cache` |
| `reports/` | Generated HTML boards (local only) |

Single file, edited in place — git history is the versioning.

## Running

```powershell
python "C:\Basketball Guru\BasketballGuru.py"                              # tonight, deploy + push
python "C:\Basketball Guru\BasketballGuru.py" --no-deploy --no-notify      # local board only
python "C:\Basketball Guru\BasketballGuru.py" --date 2026-01-15 --backtest # rebuild a past slate, grade it
python "C:\Basketball Guru\BasketballGuru.py" --calibrate 2025-10-22 2026-04-12   # model check over a range
python "C:\Basketball Guru\BasketballGuru.py" --backfill 2026              # pull every box score of a season
```

## Model in one paragraph

Minutes first: each player's base (recent games with this team weighted up,
DNP-coach's-decision counted as zeros, last season when there is no current
data) and the team is filled back to 241.5 minutes — short-handed teams hand
the missing minutes to players with room to grow, weighted toward the missing
man's position; overfull ones trim from the end of the bench. Starters lose a
little in projected blowouts. Production = per-minute rate (both seasons,
recent weighted up, lightly shrunk to the position average) × minutes, then
every team is scaled so its players add up to
what the team should produce tonight: points = the DK implied team total,
rebounds/assists/threes = offense × opponent / league × the pace Vegas
implies. A star sitting raises everyone else's share; a star returning lowers
it. The opponent only enters through those team totals — there is no
defense-vs-position (DvP) factor. Outcomes are half negative binomial, half
normal, with Var = a·mu^c fitted on backtest residuals (PRA: 10.84·mu^0.591)
plus `BOOST_VAR_K`·boost². A prop on a DNP is voided, so the board projects the night
he plays. Players with no NBA games at all (rookies, players back from overseas)
are projected from this season's preseason: 70% of their preseason minutes,
rates shrunk hard toward 80% of the position average — and no boost, since
there is no NBA average to compare against.

## Calibration (2025-26 regular season replay, Oct 2026)

Every slate rebuilt from only the earlier box scores, the closing DK line and
the injury DNPs (coach's-decision DNPs stay in the pool — nobody knew those):

| 2025-26 regular season (20,397 rotation player-games) | Model | Last-10 avg |
|---|---|---|
| PRA error per player | **6.21** | 6.61 |
| ...when a rotation teammate is out | **6.40** | 7.07 |
| First month only (the launch window) | **6.36** | 6.74 |
| Bias | -0.01 | |

- Grades sort: A+ 45.8 / A 38.4 / B+ 33.5 / B 28.1 / C 22.9 / D 14.5 actual PRA, each within ~1 of projected
- Boost +4 or more cleared the L10 line 73.9% (model said 73.5%); -4 or less stayed under 75.1%
- P(over) is calibrated in every bucket (Brier 0.231 vs 0.25 coin flip) -- that needed the NB/normal
  mixture plus `BOOST_VAR_K`; the NB alone under-called Overs by ~3 points
- DvP was removed (Oct 13): it added nothing — every number above is with it gone, and the Brier
  improved slightly (0.2311 -> 0.2310). Don't re-add a matchup factor without a calibration run that beats this
- Rookie fallback (Oct 17): without it, 2025-26 opening week had 43 appearances (12 at 20+ min) by
  players the model could not see, and their minutes inflated teammates. With it (`PRE_*` knobs tuned on
  the 2025-26 opening fortnight): teammates' first-game error 7.08 -> 7.00 and bias -0.75 -> -0.08,
  rookies themselves unbiased (+0.06, error ~8.9), full-season numbers unchanged
- **Against real DraftKings lines (Oct 17)** — 1,179 2025-26 lines ESPN still had (92% playoffs, 90 regular
  season): DK missed by 6.20 a player, the model 6.45, the last-10 average 6.67. The model's 3+ point
  disagreements with DK went 49%; its own More% had no relation to what happened; Less won 54% across the
  board. So the 73% boost numbers above were wins against our own stand-in line, not against DK. The board
  now uses DK's line wherever DK prices the player: the boost is model minus DK line, and the odds use
  mean = DK + `DK_MODEL_W`·(model − DK) with `DK_MODEL_W` = 0.1 (fitted; 0.0 calibrates as well). MORE / LESS
  tags are the model's opinion and are graded vs DK every night — they have not earned trust yet

## Data sources (all free, no keys)

- **ESPN site API** `site.api.espn.com/apis/site/v2/sports/basketball/nba` — scoreboard (with DraftKings spread/total), summary (box score + `pickcenter` closing DK line), team schedules, rosters, injuries
- stats.nba.com and cdn.nba.com refuse/hang from this PC and are known to block GitHub's runners — do not build on them
- **ESPN core API** `sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{id}/competitions/{id}/odds/100/propBets` — DraftKings player props, free. PRA main line = type id `90` ("Total Points, Rebounds, and Assists"), two items per player (over/under, unlabeled — only the line is used). Posted a few days before a game; dropped some time after it
- PrizePicks / Underdog / DK sportsbook APIs all 403 — not needed now. The Odds API also sells props but its credits belong to Hockey Guru; do not spend them here

## Key config (top of BasketballGuru.py)

- `PRA_GRADES` — absolute grade cut-offs on projected PRA
- `TOP_SHOWN` / `MIN_PROJ_MINUTES` — cards shown before "Show all"
- `MAIN_SLATE_START_ET` — what counts as the DK main slate for the toggle
- `INCLUDE_PRESEASON` — exhibition slates used only when no regular-season games (camp dry run)
- Engine knobs (`RATE_K`, `RATE_DECAY`, `PRI_W`, `MIN_RECENT_*`, `FILL_*`, `TRIM_BETA`, `NORM_*`, `BLOWOUT_*`, `VAR_MODEL`, `BOOST_VAR_K`, `PRE_*`) — tuned with `--calibrate`; move one only if the calibration report says so
- `BOOST_CALL` — model-vs-line gap that counts as a More / Less call on the board and scorecard
- `DK_MODEL_W` — the model's share of the say against a DK line in the odds
- `GITHUB_TOKEN` — env var (repo secret on Actions); `NTFY_TOPIC` — `basketball-guru`

## Open items

- **Revisit on Mon Oct 19, 2026 (day before opening night):** for players with NO DK line (mostly deep
  bench), the card still says "vs L10", but until he has 5 games this season the fallback line is his
  last-40-game average (`ref_pra` in `finish_row`). The user chose to leave the label as is for now —
  decide then whether to relabel it. Players with a DK line are labelled "DK line" correctly.
- **Re-fit `DK_MODEL_W` after ~3 weeks of regular season:** `--calibrate 2026-10-20 <date>` prints an
  "Against DraftKings' real line" section from the archived lines (miss, hit rate of 2+/3+/4+ gaps, and
  the miss at each weight). The 0.1 came from a playoff-heavy sample.

- **Pick6 (Oct 17):** all Pick6 references were removed from the board at the user's request — revisit
  after going live with the group. The beginner-card mockup had an "If Pick6 shows…" strip (DK's line
  ±2 with a verdict each); it was dropped too.

## Gotchas

- Slate date is the **Eastern** date; ESPN's scoreboard is keyed the same way.
- ESPN's injury feed only says Out / Day-To-Day; a fresh news blurb saying "won't play" upgrades DTD to OUT (`RULED_OUT_RE`). Late scratches after the last cron are missed.
- Until a player has 5 games this season his line proxy is his last-40-game average, not last April's final 10.
- Some scheduled 2025-26 ids 404 on ESPN (NBA Cup placeholders); finished seasons only fetch games marked completed.
- ESPN abbreviations are the team keys everywhere: GS, NY, NO, SA, UTAH, WSH.
- Predictions freeze per game once it tips; anything logged after is flagged `late` and not scored.
- DK lines only refresh for games that have not tipped, so `props_<season>.json` holds the last pre-tip line.
  An early-morning run may find no props posted yet; the board then falls back to the average line.
- `.claude/launch.json` serves `reports/` on :8766 for previewing boards in the app.
