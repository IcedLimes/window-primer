# window-primer

[![tests](https://github.com/IcedLimes/window-primer/actions/workflows/tests.yml/badge.svg)](https://github.com/IcedLimes/window-primer/actions/workflows/tests.yml)

Claude subscriptions meter usage in **5-hour windows** that start with your first message.
If you usually start at 1 pm and run out by 3:30, you wait until 6 pm. If a tiny message had
opened the window at 10 am instead, it would reset at 3 pm — right when you need it.

window-primer learns *when* and *how hard* you use Claude from your Claude Code history,
picks the ping times that would have cut your lockouts the most, checks that choice against
held-out weeks, and then sends a one-word Haiku message at those times — from your machine,
and optionally from a Railway cron job so it works while your computer is asleep.

```
            00    03    06    09    12    15    18    21          (example output)
Mon (5d)  ·                     ▁▂▃▃▂▁ ▂▄▆█▇▅▃▂▄▆▇▆▄▂▁
                             ▲             ▲              ping 09:21, 16:31
Tue (5d)                        ▁▂▂▁·  ▃▅▇▇▆▄▂▁▃▅▆▅▃▁·
                             ▲             ▲              ping 09:21, 16:31
…
Held-out weeks (plan without that week, test on it):
  global  ≤2 ping/day   lockout 3h10m → 1h50m   over-budget windows 6 → 4
```

It is honest about whether it helps: if no schedule beats "don't ping" on held-out weeks,
it schedules nothing.

## Requirements

- Linux with a systemd user session (for the local timers)
- Python ≥ 3.11 (standard library only)
- [Claude Code](https://code.claude.com) signed in with a Claude subscription
- Optional: a [Railway](https://railway.com) account and the Railway CLI for the cloud runner

## Install

```sh
git clone https://github.com/IcedLimes/window-primer.git ~/Projects/window-primer
cd ~/Projects/window-primer
bin/primer install
primer report
```

`primer install`:

- links `~/.local/bin/primer` and the Claude Code plugin (`~/.claude/skills/primer` → `primer@skills-dir`)
- adds a statusline to `~/.claude/settings.json` (backs it up first; skipped if you already have one)
- installs `primer-ping.timer` and a daily `primer-replan.timer` as systemd user units

`primer uninstall` reverses all of it and keeps your data in `~/.local/share/window-primer`.

## Use

| Command | |
|---|---|
| `primer status` | open window, % used, reset time, weekly %, next pings, recent ping outcomes |
| `primer report` | weekday × time heatmap, calibrated budget, schedule, replay, held-out-week results |
| `primer refresh` | re-ingest, re-plan, update the timers (also runs daily and on session start if stale) |
| `primer ping [--force]` | open a window now (skips if one is known to be open) |
| `primer skip DATE… / --remove / --clear` | days with no pings, e.g. `primer skip tomorrow 2026-12-25` |
| `primer config [key [value]]` | settings, e.g. `primer config ping_hours '[7, 23]'` |
| `primer cloud link\|sync\|pull\|cron` | manage the cloud runner |

In Claude Code: `/primer:status`, `/primer:report`, `/primer:replan`, `/primer:ping`, or just ask
"when does my limit reset?".

Useful settings: `ping_hours` (only ping inside these local hours), `max_pings_per_day`,
`mode` (`auto` | `global` | `pooled` | `weekday`), `pool_kappa`, `stop_at`, `budget_since`,
`budget_override_usd`, `lookback_days`, `impute_lockout_factor`.

When your window is nearly spent with a long wait left, the statusline and `primer status`
point out how much a `/limit-reset` would save — but only if the weekly limit has room and you
haven't used one in the past week, since it's rationed.

## How it decides

1. Every response in `~/.claude/projects/**` becomes an API-equivalent cost (its "intensity"),
   kept in a local sqlite store so it outlives Claude Code's transcript cleanup.
2. The per-window budget is calibrated from your real limit-hit messages and live utilization
   readings (statusline + pings). Limits change every few months, so the planner hedges across
   all estimates with a one-week half-life.
3. A simulator replays your history under candidate ping times — windows open at the first
   request, floored to 10 minutes, and last 5 hours; a ping inside an open window does nothing.
   Work you were locked out of is estimated, so the hours a better ping would rescue count.
4. Same-time-every-day, partially pooled (empirical Bayes) and per-weekday schedules with 1–2
   pings are cross-validated week by week; the simplest one that actually helps on held-out
   weeks wins.
5. Once pings run, the report compares real limit hits and lockout time before vs since.

Details and the evidence behind each choice: [docs/DESIGN.md](docs/DESIGN.md).

## FAQ

**Does it only learn from the times I hit the limit?** No. It replays *all* your usage, response by
response. Limit hits and live statusline readings are only used to measure how big a window is. If you
stop before the limit, `stop_at` (default 0.95) makes it plan against the budget you actually use, and
windows you left early near the limit are treated as lockouts. Recent Claude Code also waits at the
limit and continues by itself after the reset, so stopping early matters less than it used to.

**Why not just ping at the start of every 5-hour window?** It was tested on real history: it tied the
optimized schedule at best, cost ~5 pings a day instead of ~1, and was worse than no pings on about 1
day in 11. Its reset times drift an hour later every day, so they can't follow your routine. The report
shows how it would have done on your own data, every time.

**I upgraded my plan — will it notice?** Yes: an upgrade, downgrade or weekly reset shows up in the
live readings, and the budget is re-estimated from data after it. If a change slips through, run
`primer config budget_since 2026-10-01T21:30`.

## Cloud runner (Railway)

Local timers can't fire while your machine sleeps. The cloud runner is a Railway cron service
built from `cloud/Dockerfile`; it uses the same ping and decides in your local time zone
whether a ping is due, so DST and per-weekday plans just work. Its cost is a few seconds of
compute per run.

1. Install and sign in to the Railway CLI (`npm i -g @railway/cli`, `railway login`).
2. Create an empty service, then set its Dockerfile path to `cloud/Dockerfile`
   (Settings → Build, or the variable `RAILWAY_DOCKERFILE_PATH=cloud/Dockerfile`).
3. From the repo root:
   `railway up --project <project> --environment <environment> --service <service>`
4. `primer cloud link --project <project> --environment <environment> --service <service>` —
   sets the cron and pushes your schedule; later `primer refresh` runs keep it in sync.
5. Run `claude setup-token` **in your own terminal** and add the result to the service's
   Variables as `CLAUDE_CODE_OAUTH_TOKEN`. If the dashboard shows it as a staged change,
   click **Deploy** to apply it.
6. Optional test: set `PRIMER_FORCE=1` and a `*/5 * * * *` cron, wait for one run, then
   set them back (`PRIMER_FORCE=0`, `primer cloud sync`). `primer status` shows the result.

The token can spend your subscription — keep the Railway project private. The local timer
stays on as a backup; if both fire, the second ping lands in an open window and does nothing.

## Limits

- Local pings need the machine awake and you logged in; the cloud runner covers both.
- Usage on claude.ai or other devices is only seen through limit hits and live readings.
- Pinging moves your 5-hour windows; it doesn't add weekly quota.
- It reads fields Claude Code emits today (`rate_limit_event` in `stream-json`, `rate_limits`
  in statusline input) that aren't formally documented and may change.

## Development

```sh
python3 -m unittest discover tests
```

Not affiliated with or endorsed by Anthropic.

## License

[MIT](LICENSE)
