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
    store.py      sqlite: events, limit_hits, observations, pings, files
    ingest.py     incremental transcript scan → events + limit hits
    sim.py        5-hour window simulator (10-min bins)
    planner.py    calibration, optimisation, backtest, cross-validation
    render.py     terminal heatmap / report / status
    ping.py       run the ping, parse rate_limit_event, log it
    scheduler.py  systemd user units (ping timer + daily replan timer)
    statusline.py statusline: records live window state, prints a status line
    cloud.py      Railway cron runner + local sync of schedule and results
    cli.py        subcommands
  cloud/Dockerfile                 image for the cloud runner
  tests/                           unittest suite
~/.local/share/window-primer/      primer.db, config.json, plan.json
~/.config/systemd/user/            primer-ping.{service,timer}, primer-replan.{service,timer}
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
recency-weighted distribution of estimates** (readings count double). The median is shown
for display and replay.

**Objective per window**, recency-weighted (half-life 21 days), averaged over the budget
distribution: `max(0, L − B) + 0.2·max(0, L − 0.8B)`, with a tiny `L²/B` tie-break that
prefers balanced windows. With no budget at all it falls back to Σ L².

**Optimisation.** Try "no ping" and every 10-minute slot, simulating the whole history
continuously (late-night windows carry across midnight). The objective curve is smoothed
over ±30 min so a small wobble in your start time doesn't turn a good slot into a bad one.
Optionally add a second ping per day. Pings that make no measurable difference on a weekday
are dropped.

**Choosing the schedule shape by cross-validation.** Per-weekday schedules only have ~5
examples per decision and overfit badly; in testing they made held-out weeks *worse*. So
`mode: auto` runs leave-one-week-out cross-validation for {global, per-weekday} × {1, 2}
pings/day and keeps the simplest variant within 1% of the best held-out score, but only if
it beats not pinging by `min_gain_frac`. Otherwise nothing is scheduled.

**Ping timing.** Pings fire 1 minute into their slot (e.g. `09:21`) so the API's 10-minute
floor lands on the intended slot.

## 4. Runtime

- **primer-ping.timer** — one `OnCalendar=` line per planned ping, `AccuracySec=1s`, and
  deliberately no `Persistent=`: a 07:01 ping missed while asleep must not fire at noon.
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

## 5. Cloud runner

Railway cron runs in UTC, at least 5 minutes apart, and can start a few minutes late. The
cron expression is the smallest single expression covering every planned time under both
the standard and daylight UTC offsets; the runner decides in local time whether a ping is
actually due (late by up to 30 min is still accepted, and up to ~8 min still lands in the
intended slot). Local `primer refresh` pushes the schedule (`PRIMER_SCHEDULE`) and cron via
the Railway CLI, retrying when the network isn't back yet, and `primer status` pulls ping
results back from the service logs. The local timer stays on as a backup; a second ping in
an open window is a no-op.

## 6. Limits

- Local pings need the machine awake and the user logged in (`loginctl enable-linger` covers
  logged-out, nothing covers suspend) — the cloud runner covers both.
- claude.ai / phone usage is only seen through limit hits and live readings.
- Pinging doesn't add weekly quota.
- Replays can't see work that would have happened during real lockouts, so gains are
  understated.
- Relies on fields Claude Code emits today (`rate_limit_event`, statusline `rate_limits`) that
  aren't formally documented; everything degrades gracefully if they disappear.
