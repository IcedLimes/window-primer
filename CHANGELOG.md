# Changelog

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
