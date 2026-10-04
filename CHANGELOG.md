# Changelog

## Unreleased

- Partial pooling (empirical Bayes) between shared and per-weekday schedules.
- Work blocked by past lockouts is estimated so the optimiser can see it.
- Budget estimates fade with a 7-day half-life; limits change often.
- `/limit-reset` advisor in the statusline and `primer status`.
- `primer skip` for days off, honoured locally and by the cloud runner.
- The report compares real limit hits and lockout time before vs since pings started.
- Planner about twice as fast.
- Detects plan upgrades/downgrades and limit resets from live readings and re-estimates the budget
  from data after them (`budget_since` to set one by hand).
- Reads through stale statusline values from idle sessions and drops off-grid glitches.
- Models stopping before the limit: plans against `stop_at` × the budget and treats windows left
  early near the limit as lockouts.
- The report scores a ping-at-every-window-start (keep-alive) strategy against the plan; it isn't
  used because it didn't beat scheduled pings on real history.

## 1.0.0 — 2026-10-01

First release. Linux only (systemd user timers).

- Learns when and how hard you use Claude from Claude Code transcripts and keeps its own
  history in a local sqlite store.
- Calibrates the per-window budget from real limit-hit messages and live utilization
  readings, hedging across all estimates.
- Replays your history under candidate ping times and picks a schedule by leave-one-week-out
  cross-validation; schedules nothing if no schedule helps on held-out weeks.
- Sends a one-word Haiku ping (hooks and plugins disabled) from a systemd user timer and
  verifies from the API response whether it opened a window.
- Claude Code plugin: `/primer:status`, `/primer:report`, `/primer:replan`, `/primer:ping`,
  a usage-windows skill and a SessionStart hook that re-plans when the plan is stale.
- Statusline showing 5-hour and weekly usage, reset time and the next ping.
- Optional Railway cron runner so pings fire while the machine is asleep.
- Counts a lockout once even when Claude Code logs several limit messages for it, and only
  sends scheduled pings when a planned time is actually due.
