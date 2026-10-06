---
name: usage-windows
description: Answer questions about the user's Claude usage limits and 5-hour windows using window-primer — when the current window resets, how much is used, when the next automatic ping fires, when they usually work, or whether pinging is helping. Use for "when does my limit reset", "how much usage do I have left", "when is my next ping", "change my ping times", "am I near my weekly limit".
---

# Usage windows (window-primer)

window-primer learns when the user works from their Claude Code transcripts and sends a tiny
Haiku message at planned times so 5-hour windows reset mid-session instead of after it.

Use the `primer` CLI (on PATH):

- `primer status` — open window, % used, reset time, weekly %, next pings, recent ping outcomes.
- `primer report` — weekday × time heatmap, calibrated budget, schedule, replay and cross-validation.
- `primer refresh` — re-plan now and update the systemd timer.
- `primer ping` — open a window now (no-op if one is already open; `--force` to send anyway).
- `primer config [key [value]]` — settings. Useful keys: `ping_hours` (e.g. `[7,23]` if the
  machine sleeps at night), `max_pings_per_day`, `mode` (`auto`/`global`/`weekday`),
  `budget_override_usd`. Run `primer refresh` after changing one.

Facts to keep straight when explaining:
- A window opens at the first request when none is open, floored to 10 minutes, and lasts 5 hours.
- A ping inside an open window does nothing. Pings don't add weekly quota.
- Local pings only fire while the machine is awake and the user is logged in (systemd, launchd or
  Task Scheduler); the optional Railway cloud runner covers the rest.
- "Budget" is in API-equivalent dollars per window — a relative measure, not money spent.
