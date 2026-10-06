# Design

Goal: learn when you use Claude (time of day × weekday × intensity) from the last ~5 weeks
of Claude Code transcripts, and send a tiny "ping" at the moment that best places the
5-hour usage-window boundaries, so resets land in the middle of heavy sessions instead of
after them.

## 1. What the design rests on

These were checked against a real account's transcripts and live API responses while
building this; anything that might change is read defensively.

| Observation | How it was established |
|---|---|
| Usage history is on disk | `~/.claude/projects/**/*.jsonl` holds every API response with timestamp, model and token usage. Subagent transcripts live in nested directories and must be included. |
| Responses are duplicated across lines | The same `message.id` appears once per content block, so events are deduplicated by id. |
| Limit hits are recorded | Synthetic assistant messages such as `"You've hit your session limit · resets 6:50pm (America/Los_Angeles)"`. They give the exact reset time of a window that ran out. |
| A window starts at the first request **floored to 10 minutes** and lasts 5 h | Limit-hit reset times line up exactly with first-request times rounded down to 10 min (e.g. first request 13:52 → "resets 6:50pm"), and the live `resetsAt` is always a multiple of 600 s. |
| Other surfaces share the window | Some limit-hit windows had no Claude Code activity at their start, i.e. they were opened by claude.ai or another device. |
| A ping is cheap and reports the window it landed in | `claude -p --safe-mode --model haiku --tools "" …` takes ~2 s, costs ≈$0.004 API-equivalent, and its `stream-json` output contains a `rate_limit_event` with `five_hour.resetsAt` and utilization, so every ping verifies whether it opened a window. |
| Live window state is available to statuslines | Claude Code passes `rate_limits.five_hour.{used_percentage,resets_at}` (and `seven_day`) to the statusline command. |
| Plugins can't provide a statusline or run on a timer | Plugin components are commands/skills/hooks/MCP; `statusLine` is settings-only, and nothing in a plugin runs while Claude Code is closed, so scheduling uses systemd (and optionally a cloud cron). |
| User hooks would fire on pings | A Stop hook that plays a sound would go off at 7 am, so pings run with `--safe-mode` and `disableAllHooks`. |
| Per-model price tiers | Taken from Claude Code's model catalog; used only as relative weights (see §3). |
| Limits change often | Anthropic has run promotions, doubled 5-hour limits (May 2026) and cut them again (September 2026), so old evidence about the budget goes stale within weeks. |
| One lockout can log several limit messages | Claude Code printed three "You've hit your session limit" messages for a single window; hits are deduplicated by reset time. |
| Sessions can be reset once in a while | `/limit-reset` (Claude Code 2.1.x) clears the current 5-hour limit; it is rationed (reportedly once a week) and draws on the weekly limit. |

## 2. Architecture

```
window-primer/                     ← repo == Claude Code plugin root
  .claude-plugin/plugin.json       plugin manifest ("primer")
  commands/                        /primer:status, :report, :replan, :ping
  hooks/hooks.json                 SessionStart → `primer refresh --if-stale 12` (async)
  skills/usage-windows/SKILL.md    lets Claude answer "when does my limit reset?" etc.
  bin/primer                       CLI entry (python3 -m primer)
  primer/
    config.py     paths + config.json defaults
    pricing.py    model → price tier → API-equivalent $ per response ("intensity")
    store.py      sqlite: events, limit_hits, observations, pings, resets_used, files
    ingest.py     incremental transcript scan → events + limit hits
    sim.py        5-hour window simulator (10-min bins)
    planner.py    calibration, optimisation, backtest, cross-validation
    render.py     terminal heatmap / report / status
    ping.py       run the ping, parse rate_limit_event, log it
    statusline.py statusline: records live window state, prints a status line
    cloud.py      Railway cron runner + local sync of schedule and results
    system.py     OS differences: paths, interpreter, launchers, locking, time zones
    scheduler.py  picks a backend: backends/systemd.py, launchd.py, taskscheduler.py
    winzones.py   Windows → IANA time-zone names (CLDR)
    cli.py        subcommands
  cloud/Dockerfile                 image for the cloud runner
  tests/                           unittest suite
<data dir>/                        primer.db, config.json, plan.json  (per-OS location, see README)
```

```
transcripts ──ingest──► events(ts, $) ─┐
limit-hit messages ────► limit_hits ───┼─► budget estimates ─► cross-validated optimiser ─► plan.json
statusline / pings ────► observations ─┘                                                       │
                                                       ┌───────────────────────────────────────┤
                                                       ▼                                       ▼
                                             primer-ping.timer (local)          Railway cron (optional)
                                                       │                                       │
                                                       └──────── rate_limit_event ◄────────────┘
```

## 3. Algorithm

**Intensity.** Each response → API-equivalent USD using its model's tier (input, output,
5m/1h cache write, cache read). Plan limits drain roughly with compute, so list price is a
good *relative* weight; the absolute scale is calibrated away. In testing, windows that
Claude Code alone filled agreed to within ~15% under this weighting, while down-weighting
cache reads made them disagree more.

**Window simulator.** Absolute 10-minute bins (DST-safe). A window opens at the first bin
with activity or a ping while none is open and covers 30 bins; a ping inside an open window
does nothing, as in reality. Window starts known from limit hits or live readings that
have no Claude Code activity are replayed as zero-cost "external" events.

**Budget calibration.** Each limit hit gives `load in [reset − 5h, hit]`; each live reading
with ≥ 15 % utilization gives `load / utilization` (one per window). Usage outside Claude
Code is invisible here and only makes these estimates read low, and limits/model mix
drift over time, so the planner doesn't trust a single number: it **hedges across the
recency-weighted distribution of estimates** (readings count double, half-life 7 days — shorter
than the 21-day half-life for usage patterns, because limits change faster than habits). The
median is shown for display and replay.

**Plan and limit changes.** A plan upgrade changed this user's budget about 4x overnight, and
averaging old and new estimates gave a number wrong for both. A change shows up in live readings as
the weekly counter dropping by 20+ points while its scheduled reset stays put (an upgrade, downgrade
or weekly reset); a new window also starts at that moment. Calibration then uses only estimates since
the latest change (`budget_since` sets one by hand), older windows are judged against their own
period's budget, and the replay forces a window boundary at the change.

**Stale readings.** Every open Claude Code session feeds the statusline, and an idle one keeps
re-printing its last-known values every 60 s — 97% of this user's readings were such repeats. Only the
first appearance of a value counts; utilization estimates use the moment the window's highest value
was *first* reported, and off-grid reset times (real ones are multiples of 10 minutes) are dropped.

**Stopping early.** People stop before the limit so they aren't cut off mid-response, so hard limit
hits under-count the moments a reset was needed. Plans use an effective budget of `stop_at` × the
budget (default 0.95), and a window counts as a *soft lockout* when you left it 20+ minutes before its
end with utilization ≥ `stop_at` (from live readings), or — for older windows without readings — with
load ≥ `stop_at` × that period's budget *and* activity resuming within 30 minutes after the reset
(without that test a late-night stop is indistinguishable from bedtime). Soft lockouts get the same
blocked-work estimate as hard ones. On this user's 5.7 weeks none were found, three ways (strict rule
at several thresholds and budgets, return-right-after-reset rate, stop hazard at high load); the rule is
there to catch it as statusline data accumulates. Interactive Claude Code (v2.1.234+) also waits at the
limit and continues the task by itself after the reset, which takes away much of the reason to stop early.

**Blocked work.** During a real lockout the transcripts show nothing, which would teach the
optimiser that those hours don't matter — exactly the hours a better ping would rescue. So
each lockout is filled at half the pace of the 70 minutes before the hit (for at most 3 h,
`impute_lockout_factor`). Calibration only ever sees observed usage. The schedule choice was
the same with the factor at 0, 0.25 and 0.5; only the size of the estimated gain changes.

**Objective per window**, recency-weighted (half-life 21 days), averaged over the budget
distribution: `max(0, L − B) + 0.2·max(0, L − 0.8B)`, with a tiny `L²/B` tie-break that
prefers balanced windows. With no budget at all it falls back to Σ L².

**Optimisation.** Try "no ping" and every 10-minute slot, simulating the whole history
continuously (late-night windows carry across midnight). The objective curve is smoothed
over ±30 min so a small wobble in your start time doesn't turn a good slot into a bad one.
Optionally add a second ping per day. Pings that make no measurable difference on a weekday
are dropped.

**Partial pooling.** Between "one schedule for every day" and "each weekday on its own" sits
empirical-Bayes shrinkage: each weekday's gain curve is blended with the all-days curve with
weight κ/(n+κ), where n is how many of that weekday are in the history (κ = `pool_kappa`,
5 days). A weekday drifts from the shared times only as far as its own evidence justifies.

**Choosing the schedule shape by cross-validation.** Per-weekday schedules only have ~5
examples per decision and overfit badly; in testing they made held-out weeks *worse*. So
`mode: auto` runs leave-one-week-out cross-validation for {global, pooled, per-weekday} ×
{1, 2} pings/day and keeps the simplest variant within 1% of the best held-out score, but only
if it beats not pinging by `min_gain_frac`. Otherwise nothing is scheduled. With five weeks of
history the ranking has consistently been global > pooled > per-weekday; pooling is there to
take over as history grows.

**Why not ping at the start of every window?** A keep-alive chain (ping whenever a window ends, so
windows tile time and a reset is always under 5 hours away) was simulated on this user's history
against no pings and the cross-validated schedule, across budgets and `stop_at` values, with all 30
possible chain phases and leave-one-week-out validation. It never clearly won: on held-out weeks it
tied the scheduled pings (+48 min of lockout over 35 days, 95% interval −172 to +282), needed ~4.8
pings a day instead of ~1, and was worse than no pings on about 9% of days — a boundary can also land
where it concentrates a heavy stretch into one window. Because 24 h isn't a multiple of 5 h, its reset
times drift an hour later each day and can't be aimed at a routine; its result swung by 90–145 min
depending on the phase. The report keeps scoring it (`Ping at every window start instead…`) so the
evidence stays visible as habits or limits change.

**Spending `/limit-reset` well.** The statusline and `primer status` suggest the reset only
when it buys a lot: the window is ≥ 90 % used with ≥ 45 min left, the weekly limit is under
90 %, and no reset shows up in the transcripts for the past week.

**Ping timing.** Pings fire 1 minute into their slot (e.g. `09:21`) so the API's 10-minute
floor lands on the intended slot.

## 4. Runtime

- **primer-ping.timer** — one `OnCalendar=` line per planned ping, `AccuracySec=1s`, no
  `Persistent=`. systemd still fires a timer late after resume from suspend, and immediately when
  its schedule changes to include a time already past today, so the scheduled ping first checks
  that a planned time was within the last 15 minutes and otherwise exits.
- **primer ping** — skips if a live reading proves a window is already open; otherwise runs
  the ping (2 retries for networks that aren't up yet after resume) and logs whether it
  opened a window.
- **primer-replan.timer** — daily 04:05 (catches up after sleep): ingest → calibrate → plan →
  rewrite the ping timer and push to the cloud runner if anything changed. The plugin's
  SessionStart hook does the same when the plan is > 12 h old.
- **Statusline** — `5h 37% ↻9:50p · 7d 62% · ping Fri 09:21`; logs `rate_limits` whenever they
  change, which keeps calibration fresh.
- **Own data store** — Claude Code deletes transcripts after `cleanupPeriodDays` (default 30);
  the sqlite store keeps per-response history independently.

**Checking against reality.** Once scheduled pings start, `primer report` compares actual
limit hits and lockout time per week before vs since, which is the only test that counts.

**Speed.** A plan simulates ~60k schedules (6 variants × 5 folds of cross-validation). Totals
are memoised per schedule, ping times come from precomputed local midnights, and windows that
sit below every budget skip the per-budget sum; a full plan takes under 10 s.

## 5. Platforms

`primer/system.py` holds everything OS-specific; `primer/scheduler.py` picks a backend.

| | Scheduler | Missed while asleep | Notes |
|---|---|---|---|
| Linux | systemd user timers (`primer-ping.timer`, `primer-replan.timer`) | ping: fires late on resume → refused by the due check; replan: `Persistent=true` | `loginctl enable-linger` keeps them running while logged out |
| macOS | launchd LaunchAgents (`io.github.icedlimes.window-primer.*`) | both run once on wake → ping refused by the due check | launchd's PATH is bare, so the install-time PATH is copied into the job |
| Windows | Task Scheduler (`\window-primer\ping`, `\window-primer\replan`) | ping: `StartWhenAvailable=false`; replan: `true` | runs as you while logged on, via `pythonw.exe`; optional `WakeToRun` |

Shared choices:

- **One entry point, pinned interpreter.** Jobs, the statusline and the `primer` launchers run
  `<python> bin/primer.py …` with the interpreter `primer install` ran under — never whatever
  `python3` is first on a job's PATH (macOS ships 3.9; Windows may hit the Store alias). The
  stable `python3` name is pinned when it points at the same interpreter, so a distro or
  Homebrew upgrade from 3.14 to 3.15 doesn't strand the jobs.
- **Real executables, not npm wrappers.** On Windows, npm installs `claude.cmd` / `railway.cmd`;
  the ping's empty `--tools ""` and the JSON passed to Railway don't survive cmd.exe, so the
  package's own `.exe` is run instead, and ping settings go in a file rather than inline JSON.
- **Time zones without a tz database.** Stock Windows Python has no IANA database. The local
  zone comes from `tzutil /g` mapped through CLDR's `windowsZones` table, and DST offsets for
  the cloud cron come from the OS clock when `zoneinfo` can't load the zone.
- **Locking** uses `fcntl` on POSIX and `msvcrt` on Windows.
- **Verified** with the unit suite on Linux and on Windows Python 3.12 under Wine (including a
  full install → report → status → uninstall cycle against Wine's Task Scheduler); CI runs the
  suite on Linux, macOS and Windows. launchd itself is exercised only through mocks.

## 6. Cloud runner

Railway cron runs in UTC, at least 5 minutes apart, and can start a few minutes late. The
cron expression is the smallest single expression covering every planned time under both
the standard and daylight UTC offsets; the runner decides in local time whether a ping is
actually due (late by up to 30 min is still accepted, and up to ~8 min still lands in the
intended slot). Local `primer refresh` pushes the schedule (`PRIMER_SCHEDULE`) and cron via
the Railway CLI, retrying when the network isn't back yet, and `primer status` pulls ping
results back from the service logs. Days off (`primer skip`) travel with the schedule. The local
timer stays on as a backup; a second ping in an open window is a no-op.

## 7. Limits

- Local pings need the machine awake and the user logged in — the cloud runner covers both
  (Windows can also wake the PC with `wake_to_run`).
- claude.ai / phone usage is only seen through limit hits and live readings.
- Pinging doesn't add weekly quota.
- Replays can't see work that would have happened during real lockouts, so gains are
  understated.
- Relies on fields Claude Code emits today (`rate_limit_event`, statusline `rate_limits`) that
  aren't formally documented; everything degrades gracefully if they disappear.
