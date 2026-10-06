import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

_TMP = tempfile.mkdtemp()
os.environ["PRIMER_DATA_DIR"] = _TMP
# The tests assume Los Angeles local time. POSIX can switch per process; on Windows the CI job sets
# the machine's zone with `tzutil /s "Pacific Standard Time"` instead.
if hasattr(time, "tzset"):
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from primer import cloud, config, ingest, ping, planner, pricing, readings, sim, statusline, store, system  # noqa: E402

HAVE_TZDB = system.zone("America/Los_Angeles") is not None

CFG = dict(config.DEFAULTS)


def local_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


class LimitMessages(unittest.TestCase):
    def test_session_limit_with_minutes(self):
        hit = local_ts("2026-09-22 18:03")
        kind, reset = ingest.parse_limit_message(
            "You've hit your session limit · resets 6:40pm (America/Los_Angeles)", hit)
        self.assertEqual(kind, "session")
        self.assertEqual(reset, local_ts("2026-09-22 18:40"))

    def test_reset_after_midnight(self):
        hit = local_ts("2026-09-03 22:20")
        _, reset = ingest.parse_limit_message("You've hit your session limit · resets 1am (America/Los_Angeles)", hit)
        self.assertEqual(reset, local_ts("2026-09-04 01:00"))

    def test_weekly_with_date(self):
        hit = local_ts("2026-09-30 10:00")
        kind, reset = ingest.parse_limit_message("You've hit your weekly limit · resets Oct 4, 12pm (America/Los_Angeles)", hit)
        self.assertEqual(kind, "weekly")
        self.assertEqual(reset, local_ts("2026-10-04 12:00"))

    def test_legacy_epoch(self):
        self.assertEqual(ingest.parse_limit_message("Claude AI usage limit reached|1756000000", 0), ("session", 1756000000.0))

    def test_not_a_limit(self):
        self.assertIsNone(ingest.parse_limit_message("No response requested.", 0))


class Pricing(unittest.TestCase):
    def test_longest_prefix(self):
        self.assertEqual(pricing.tier_for("claude-opus-5-5"), pricing.TIERS["tier_4_20_cr_0_20"])
        self.assertEqual(pricing.tier_for("claude-opus-5"), pricing.TIERS["tier_5_25"])
        self.assertEqual(pricing.tier_for("claude-haiku-4-5-20251001"), pricing.TIERS["haiku_45"])
        self.assertEqual(pricing.tier_for("some-new-sonnet"), pricing.TIERS["tier_3_15"])

    def test_cost(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 2_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
        # opus-5: 5 + 25 + 0.5 + 6.25 (5m) + 10 (1h)
        self.assertAlmostEqual(pricing.cost_usd("claude-opus-5", usage), 46.75)


class Simulator(unittest.TestCase):
    def test_window_opens_and_closes(self):
        wins = sim.simulate([0, 5, 29, 30, 31], [1, 1, 1, 1, 1], [], 30)
        self.assertEqual([(w.start, w.end, w.load) for w in wins], [(0, 30, 3), (30, 60, 2)])

    def test_ping_inside_window_is_noop(self):
        wins = sim.simulate([0, 40], [1, 1], [10], 30)
        self.assertEqual([w.start for w in wins], [0, 40])

    def test_ping_opens_window_early(self):
        wins = sim.simulate([20, 35, 45], [1, 1, 1], [10], 30)
        self.assertEqual([(w.start, w.by_ping, w.load) for w in wins], [(10, True, 2), (45, False, 1)])

    def test_forced_start_cuts_the_open_window(self):
        wins = sim.simulate([0, 10, 20], [1, 1, 1], [], 30, forced=[15])
        self.assertEqual([(w.start, w.end, w.load) for w in wins], [(0, 15, 2), (15, 45, 1)])

    def test_keepalive_tiles_windows_back_to_back(self):
        wins = sim.simulate([5, 70, 200], [1, 1, 1], [0], 30, keepalive=True)
        self.assertEqual([w.start for w in wins], [0, 60, 180])  # grid 0, 30, 60, … regardless of activity
        self.assertTrue(all(w.by_ping for w in wins[1:]))

    def test_crossing(self):
        w = sim.simulate([0, 1, 2], [3, 3, 3], [], 30, detail=True)[0]
        self.assertEqual(sim.crossing_bin(w, 5), 1)


def synthetic_history(days=28, start="2026-09-01", work=("13:00", "23:00"), per_bin=1.0):
    """Every day: steady work from 13:00 to 23:00, per_bin $ per 10 min."""
    t0 = local_ts(f"{start} 00:00")
    bins, costs = [], []
    h0, h1 = (int(x.split(":")[0]) for x in work)
    for d in range(days):
        for slot in range(h0 * 6, h1 * 6):
            # local midnight + slot (no DST change in September)
            bins.append(sim.to_bin(t0 + d * 86400 + slot * 600))
            costs.append(per_bin)
    now = t0 + days * 86400
    return planner.History(bins, costs, datetime.fromtimestamp(t0).date(),
                           datetime.fromtimestamp(now - 1).date(), now)


class Planner(unittest.TestCase):
    def test_puts_reset_mid_session(self):
        # 10h of work at $1/10min = $60/day; a 5h window holds $30 of it, budget $25.
        # Without pings: window 13:00–18:00 ($30) then 18:00–23:00 ($30) → both over.
        # Best: open a window earlier so the first reset lands earlier and the load splits into thirds.
        hist = synthetic_history()
        budget = planner.Budget(25.0, [(25.0, 1.0)], "test")
        cfg = dict(CFG, mode="global", max_pings_per_day=1, smooth_slots=0)
        sched = planner.optimize(hist, budget, cfg)
        self.assertTrue(all(sched[wd] for wd in range(7)), sched)
        base = planner.backtest(hist, budget, cfg, {wd: [] for wd in range(7)})
        plan = planner.backtest(hist, budget, cfg, sched)
        self.assertLess(plan["overflow"], base["overflow"])
        slot = sched[0][0]
        # The ping must open a window that ends inside the session: start between 08:00 and 13:00.
        self.assertTrue(8 * 6 <= slot < 13 * 6, planner.slot_label(slot))

    def test_no_pings_when_far_under_budget(self):
        hist = synthetic_history(per_bin=0.1)  # $3 per 5h window
        budget = planner.Budget(30.0, [(30.0, 1.0)], "test")
        sched = planner.optimize(hist, budget, dict(CFG, mode="global"))
        self.assertFalse(any(sched.values()))

    def test_slot_labels_roundtrip(self):
        for slot in (0, 55, 143):
            self.assertEqual(planner.label_slot(planner.slot_label(slot)), slot)
        self.assertEqual(planner.slot_label(55), "09:11")

    def test_ping_bins_land_in_slot(self):
        day = datetime(2026, 9, 7).date()  # a Monday
        bins = planner.ping_bins({0: [55]}, day, day, local_ts("2026-09-08 00:00"))
        self.assertEqual(sim.bin_local(bins[0]).strftime("%H:%M"), "09:10")

    def test_weighted_quantile(self):
        self.assertEqual(planner.weighted_quantile([(10, 1), (20, 1), (30, 2)], 0.5), 20)


def mixed_history(days=28, start="2026-09-01"):
    """Saturdays: work 08:00–18:00. Other days: 13:00–23:00. $1 per 10 min."""
    t0 = local_ts(f"{start} 00:00")
    bins, costs = [], []
    for d in range(days):
        midnight = t0 + d * 86400
        h0, h1 = (8, 18) if datetime.fromtimestamp(midnight).weekday() == 5 else (13, 23)
        for slot in range(h0 * 6, h1 * 6):
            bins.append(sim.to_bin(midnight + slot * 600))
            costs.append(1.0)
    now = t0 + days * 86400
    return planner.History(bins, costs, datetime.fromtimestamp(t0).date(),
                           datetime.fromtimestamp(now - 1).date(), now)


class Pooling(unittest.TestCase):
    BUDGET = planner.Budget(25.0, [(25.0, 1.0)], "test")

    def plan(self, **kw):
        cfg = dict(CFG, max_pings_per_day=1, smooth_slots=0, **kw)
        return planner.optimize(mixed_history(), self.BUDGET, cfg)

    def test_weak_pooling_lets_saturday_differ(self):
        sched = self.plan(mode="pooled", pool_kappa=0.01)
        self.assertNotEqual(sched[5], sched[0], sched)
        self.assertLess(sched[5][0], sched[0][0])  # Saturday starts earlier, so it pings earlier

    def test_strong_pooling_pulls_saturday_toward_the_rest(self):
        weak, strong = self.plan(mode="pooled", pool_kappa=0.01), self.plan(mode="pooled", pool_kappa=1000)
        self.assertLessEqual(abs(strong[5][0] - strong[0][0]), abs(weak[5][0] - weak[0][0]))

    def test_weekday_mode_matches_zero_pooling(self):
        self.assertEqual(self.plan(mode="weekday"), self.plan(mode="pooled", pool_kappa=0.0))


class HistoryLoading(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        self.hit = local_ts("2026-09-22 18:00")
        self.reset = local_ts("2026-09-22 19:40")
        for i in range(12):  # $1 per 10 min for the 2 hours before the hit
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                            (f"m{i}", self.hit - 7200 + i * 600, "claude-opus-5", 1.0, 0))
        for offset in (0, 120, 300):  # one lockout, reported three times
            self.db.execute("INSERT INTO limit_hits VALUES (?,?,?,?)", (self.hit + offset, self.reset, "session", ""))
        self.now = local_ts("2026-09-25 12:00")

    def tearDown(self):
        self.db.close()

    def test_duplicate_limit_messages_count_once(self):
        hist = planner.load_history(self.db, dict(CFG, impute_lockout_factor=0), self.now)
        self.assertEqual(len(hist.hits), 1)

    def test_lockout_demand_is_imputed_but_hidden_from_calibration(self):
        cfg = dict(CFG, impute_lockout_factor=0.5, impute_lockout_max_hours=3)
        hist = planner.load_history(self.db, cfg, self.now)
        # Pace over the 70 min up to the hit: $6 in 7 slots; half of that, for the 9 slots between
        # the hit's slot and the reset.
        self.assertAlmostEqual(hist.imputed, 6 / 7 * 0.5 * 9)
        self.assertAlmostEqual(hist.cost_between(self.hit - 7200, self.reset), 12.0)
        self.assertGreater(sum(hist.act_costs), 12.0)


class Readings(unittest.TestCase):
    """Live readings: stale repeats, glitches, plan changes."""
    W = 5 * 3600

    def setUp(self):
        self.db = store.connect(":memory:")
        self.t0 = local_ts("2026-10-01 17:20")

    def tearDown(self):
        self.db.close()

    def obs(self, minutes, five, resets_min, week=None, week_resets=None):
        self.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                        (self.t0 + minutes * 60, "statusline", five, self.t0 + resets_min * 60, week, week_resets))

    def test_stale_repeats_glitches_and_cut_windows(self):
        wr = local_ts("2026-10-04 12:00")
        for m, u in ((10, 0.2), (60, 0.5), (61, 0.5), (62, 0.5), (200, 0.99), (240, 0.99), (280, 0.99)):
            self.obs(m, u, 300, 0.8, wr)
        self.obs(150, 0.3, 326)                       # off-grid glitch (resets at 22:46)
        for m, u in ((252, 0.0), (260, 0.1), (270, 0.2)):  # a new window from 21:30 (upgrade)
            self.obs(m, u, 250 + 300, 0.0, wr)
        live = readings.live_windows(self.db, self.W)
        self.assertEqual(len(live), 2)
        first = live[0]
        self.assertEqual([u for _, u in first["fresh"]], [0.2, 0.5, 0.99])  # repeats dropped
        self.assertEqual(first["end"], self.t0 + 250 * 60)                    # cut by the 21:30 window
        changes = readings.limit_changes(self.db, live)
        self.assertEqual(changes, [self.t0 + 250 * 60])                       # weekly 0.8 -> 0.0, same reset

    def test_weekly_reset_on_schedule_is_not_a_change(self):
        self.obs(0, 0.1, 300, 0.9, local_ts("2026-10-04 12:00"))
        self.obs(10, 0.1, 300, 0.0, local_ts("2026-10-11 12:00"))  # new week: reset time moved on
        self.assertEqual(readings.limit_changes(self.db, []), [])

    def test_calibration_ignores_estimates_before_a_plan_change(self):
        change = local_ts("2026-10-01 21:30")
        hist = planner.History([], [], datetime(2026, 9, 1).date(), datetime(2026, 10, 3).date(),
                               local_ts("2026-10-03 23:00"), changes=[change])
        hist.hits = []
        estimates = [{"source": "limit hit", "ts": local_ts("2026-09-20 12:00"), "budget": 40.0},
                     {"source": "utilization", "ts": local_ts("2026-10-02 12:00"), "budget": 170.0},
                     {"source": "utilization", "ts": local_ts("2026-10-03 12:00"), "budget": 200.0}]
        original = readings.budget_estimates
        readings.budget_estimates = lambda *a, **k: [dict(e) for e in estimates]
        try:
            b = planner.calibrate(self.db, hist, CFG)
        finally:
            readings.budget_estimates = original
        self.assertIn(b.point, (170.0, 200.0))
        self.assertTrue(all(v >= 170 for v, _ in b.dist))
        self.assertEqual(b.at(local_ts("2026-09-20 12:00")), 40.0)  # the old plan's budget, for old windows
        self.assertIn("since the limit change", b.source)
        self.assertAlmostEqual(b.scaled(0.9).point, b.point * 0.9)


class SoftLockouts(unittest.TestCase):
    """Windows left early near the limit."""

    def hist_with(self, active):
        bins = sorted(active)
        return planner.History(bins, [active[b] for b in bins], datetime(2026, 9, 1).date(),
                               datetime(2026, 9, 3).date(), 10_000 * sim.BIN, raw=(bins, [active[b] for b in bins]))

    def test_live_reading_stop_near_limit(self):
        # Window bins 100-130; active 100-110 then quiet; readings say 96% when the work stopped.
        hist = self.hist_with({b: 1.0 for b in range(100, 111)})
        live = [{"start": 100 * sim.BIN, "end": 130 * sim.BIN, "resets_at": 130 * sim.BIN,
                 "fresh": [(105 * sim.BIN, 0.5), (110 * sim.BIN + 30, 0.96)]}]
        ev = readings.soft_lockouts(hist, live, [], lambda t: 100, 0.95)
        self.assertEqual([(e["source"], e["stop"], e["util"]) for e in ev], [("readings", 110, 0.96)])
        self.assertEqual(readings.soft_lockouts(hist, live, [], lambda t: 100, 0.97), [])  # below threshold

    def test_transcripts_need_the_resume_after_reset(self):
        active = {b: 10.0 for b in range(100, 110)}  # $100 by bin 109, window ends at 130
        bedtime = self.hist_with(dict(active))
        w = sim.simulate(*bedtime.raw, [], 30, detail=True)
        self.assertEqual(readings.soft_lockouts(bedtime, [], w, lambda t: 100, 0.95), [])  # never came back
        active[131] = 1.0  # back 10 min after the reset
        waited = self.hist_with(active)
        w = sim.simulate(*waited.raw, [], 30, detail=True)
        ev = readings.soft_lockouts(waited, [], w, lambda t: 100, 0.95)
        self.assertEqual([(e["source"], e["start"], e["stop"]) for e in ev], [("transcripts", 100, 109)])
        demand = dict(zip(*waited.raw))
        added = readings.impute_soft(demand, ev, CFG, 10_000)
        self.assertAlmostEqual(added, 10.0 * 0.5 * 18)  # half the pace, capped at 3 h (18 bins) before the reset


class KeepAliveCompare(unittest.TestCase):
    def test_reports_every_phase_and_real_ping_rate(self):
        hist = synthetic_history(days=14)
        budget = planner.Budget(25.0, [(25.0, 1.0)], "test")
        ka = planner.keepalive_compare(hist, budget, dict(CFG))
        self.assertAlmostEqual(ka["pings_per_day"], 4.8)
        self.assertLessEqual(ka["lockout_min"], ka["lockout_mean"])
        self.assertLessEqual(ka["lockout_mean"], ka["lockout_max"])


class ResetAdvice(unittest.TestCase):
    def test_advises_only_when_worth_it(self):
        now = 1_000_000.0
        self.assertAlmostEqual(planner.reset_advice(0.97, now + 7200, 0.5, None, now), 120)
        self.assertIsNone(planner.reset_advice(0.6, now + 7200, 0.5, None, now))        # room left
        self.assertIsNone(planner.reset_advice(0.97, now + 1200, 0.5, None, now))       # resets soon anyway
        self.assertIsNone(planner.reset_advice(0.97, now + 7200, 0.95, None, now))      # weekly nearly gone
        self.assertIsNone(planner.reset_advice(0.97, now + 7200, 0.5, now - 86400, now))  # used this week


class RealWorld(unittest.TestCase):
    def test_waits_for_a_ping_that_opened_a_window(self):
        db = store.connect(":memory:")
        db.execute("INSERT INTO pings VALUES (?,?,?,?,?)", (local_ts("2026-09-15 09:21"), "cloud", "inside-window", None, ""))
        hist = planner.History([], [], datetime(2026, 9, 1).date(), datetime(2026, 9, 28).date(), local_ts("2026-09-29 00:00"))
        self.assertIsNone(planner.real_world(db, hist)["pings_started"])
        db.close()

    def test_before_vs_since(self):
        db = store.connect(":memory:")
        start = local_ts("2026-09-15 09:21")
        db.execute("INSERT INTO pings VALUES (?,?,?,?,?)", (start, "timer", "opened", start + 18000, ""))
        hist = planner.History([], [], datetime(2026, 9, 1).date(), datetime(2026, 9, 28).date(),
                               local_ts("2026-09-29 00:00"),
                               hits=[(local_ts("2026-09-03 18:00"), local_ts("2026-09-03 20:00")),
                                     (local_ts("2026-09-08 18:00"), local_ts("2026-09-08 19:00"))])
        rw = planner.real_world(db, hist)
        self.assertEqual((rw["before"]["hits"], rw["since"]["hits"], rw["opened"]), (2, 0, 1))
        self.assertAlmostEqual(rw["before"]["lockout_min_per_week"], 180 / ((start - local_ts("2026-09-01 00:00")) / 86400) * 7)
        db.close()


class PingRun(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        self.cfg = dict(CFG, claude_bin=sys.executable, ping_retries=1)

    def tearDown(self):
        self.db.close()

    def fake(self, reset_offset_s, util=0.0):
        now = time.time()
        reset = (now // 600) * 600 + reset_offset_s
        out = "\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init"},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": util, "resetsAt": reset},
                "seven_day": {"utilization": 0.5, "resetsAt": reset + 86400}}}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "k"}])
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")

    def test_opened(self):
        outcome, _ = ping.run(self.cfg, self.db, runner=self.fake(5 * 3600))
        self.assertEqual(outcome, "opened")
        self.assertEqual(self.db.execute("select count(*) from observations").fetchone()[0], 1)

    def test_inside_existing_window(self):
        outcome, _ = ping.run(self.cfg, self.db, runner=self.fake(2 * 3600, 0.4))
        self.assertEqual(outcome, "inside-window")

    def test_off_grid_glitch_does_not_count_as_open_window(self):
        store.add_observation(self.db, time.time(), "statusline", 0.3, (time.time() // 600) * 600 + 3600 + 160)
        self.assertIsNone(ping.active_window_until(self.db, time.time()))

    def test_skips_when_window_known_open(self):
        store.add_observation(self.db, time.time(), "statusline", 0.2, (time.time() // 600) * 600 + 3600)
        outcome, _ = ping.run(self.cfg, self.db, runner=lambda *a, **k: self.fail("should not run"))
        self.assertEqual(outcome, "skipped")

    def test_retries_then_errors(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            return subprocess.CompletedProcess(a, 1, "", "network down")
        outcome, detail = ping.run(self.cfg, self.db, runner=boom, sleep=lambda s: None)
        self.assertEqual((outcome, len(calls)), ("error", 2))
        self.assertIn("network down", detail)

    def test_skips_days_off(self):
        cfg = dict(self.cfg, skip_dates=[time.strftime("%Y-%m-%d")])
        outcome, detail = ping.run(cfg, self.db, runner=lambda *a, **k: self.fail("should not run"))
        self.assertEqual(outcome, "skipped")
        self.assertIn("day off", detail)

    def test_command_disables_hooks_and_tools(self):
        cmd = ping.ping_command(self.cfg)
        self.assertIn("--safe-mode", cmd)
        settings = json.loads(Path(cmd[cmd.index("--settings") + 1]).read_text())
        self.assertEqual(settings, {"disableAllHooks": True})
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")


class ScheduledPingGuard(unittest.TestCase):
    def test_timer_firing_off_schedule_does_not_ping(self):
        from primer import cli
        import contextlib, io
        config.PLAN_PATH.write_text(json.dumps({"schedule": {d: [] for d in planner.WEEKDAYS}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["ping", "--scheduled"]), 0)
        self.assertIn("not due", out.getvalue())
        db = store.connect()
        self.assertEqual(db.execute("select count(*) from pings").fetchone()[0], 0)
        db.close()


class Ingest(unittest.TestCase):
    def test_dedupes_and_finds_hits(self):
        root = Path(tempfile.mkdtemp())
        (root / "proj/session/subagents").mkdir(parents=True)
        usage = {"input_tokens": 0, "output_tokens": 1000, "cache_read_input_tokens": 0}
        line = {"type": "assistant", "timestamp": "2026-09-01T20:00:00Z",
                "message": {"id": "msg_1", "model": "claude-opus-5", "usage": usage, "content": []}}
        hit = {"type": "assistant", "timestamp": "2026-09-01T23:04:39Z",
               "message": {"model": "<synthetic>", "usage": {}, "content": [
                   {"type": "text", "text": "You've hit your session limit · resets 6:50pm (America/Los_Angeles)"}]}}
        (root / "proj/a.jsonl").write_text("\n".join(json.dumps(x) for x in (line, line, hit)) + "\n")
        (root / "proj/session/subagents/b.jsonl").write_text(json.dumps(line) + "\n")
        db = store.connect(":memory:")
        ingest.ingest(db, root)
        self.assertEqual(db.execute("select count(*), sum(cost) from events").fetchone()[:], (1, 0.025))
        self.assertEqual(db.execute("select reset_ts from limit_hits").fetchone()[0], local_ts("2026-09-01 18:50"))
        self.assertEqual(ingest.ingest(db, root), 0)  # unchanged files are skipped
        db.close()

    def test_detects_limit_reset_command_not_mentions(self):
        root = Path(tempfile.mkdtemp())
        (root / "proj").mkdir()
        tag = "<command-name>/limit-reset</command-name>"
        real = {"type": "user", "timestamp": "2026-09-30T05:00:00Z",
                "message": {"role": "user", "content": tag + "\n<command-message>limit-reset</command-message>"}}
        quoted = {"type": "user", "timestamp": "2026-09-30T06:00:00Z",
                  "message": {"role": "user", "content": [{"type": "tool_result", "content": "grep found " + tag}]}}
        (root / "proj/a.jsonl").write_text("\n".join(json.dumps(x) for x in (real, quoted)) + "\n")
        db = store.connect(":memory:")
        ingest.ingest(db, root)
        self.assertEqual(db.execute("select count(*) from resets_used").fetchone()[0], 1)
        db.close()


class Statusline(unittest.TestCase):
    def test_render_and_record(self):
        now = time.time()
        data = {"model": {"display_name": "Opus 5.5"}, "workspace": {"current_dir": "/tmp"},
                "rate_limits": {"five_hour": {"used_percentage": 37.0, "resets_at": now + 3600},
                                "seven_day": {"used_percentage": 62.0, "resets_at": now + 86400}}}
        line = statusline.render(data, now)
        self.assertIn("Opus 5.5", line)
        self.assertIn("37%", line)
        statusline.record(data["rate_limits"], now)
        db = store.connect()
        row = store.latest_observation(db)
        self.assertAlmostEqual(row["five_util"], 0.37)
        self.assertAlmostEqual(row["week_util"], 0.62)
        db.close()

    def test_iso_and_ms_timestamps(self):
        self.assertEqual(statusline._epoch("2026-10-01T08:30:00Z"), 1790843400.0)
        self.assertEqual(statusline._epoch(1790843400000), 1790843400.0)


@unittest.skipUnless(HAVE_TZDB, "needs a tz database (pip install tzdata on Windows)")
class Cloud(unittest.TestCase):
    SCHED = {"Tue": ["09:21", "16:21"], "Sun": ["09:21"]}
    TZ = "America/Los_Angeles"

    def test_cron_covers_both_dst_offsets(self):
        # PDT (UTC-7): 16:21, 23:21 UTC; PST (UTC-8): 17:21, 00:21 UTC
        self.assertEqual(cloud.cron_for(self.SCHED, self.TZ), "21 0,16,17,23 * * *")
        self.assertIsNone(cloud.cron_for({"Mon": []}, self.TZ))

    def test_due_in_local_time(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.TZ)
        tue = datetime(2026, 9, 29, 9, 24, tzinfo=tz)  # Tuesday, 3 min late
        self.assertEqual(cloud.due(self.SCHED, self.TZ, tue).strftime("%a %H:%M"), "Tue 09:21")
        self.assertIsNone(cloud.due(self.SCHED, self.TZ, datetime(2026, 9, 29, 10, 0, tzinfo=tz)))
        self.assertIsNone(cloud.due(self.SCHED, self.TZ, datetime(2026, 9, 30, 9, 24, tzinfo=tz)))  # Wed
        # winter: the PST cron run at 17:21 UTC is 09:21 local
        self.assertIsNotNone(cloud.due(self.SCHED, self.TZ, datetime(2026, 12, 1, 9, 21, tzinfo=tz)))
        # a day off
        self.assertIsNone(cloud.due(self.SCHED, self.TZ, tue, skip={"2026-09-29"}))

    def test_without_tz_database_falls_back_to_os_local_time(self):
        original = system.zone
        system.zone = lambda name: None
        try:
            self.assertEqual(cloud.cron_for(self.SCHED, self.TZ), "21 0,16,17,23 * * *")
            tue = datetime(2026, 9, 29, 9, 24)  # naive = local
            self.assertEqual(cloud.due(self.SCHED, self.TZ, tue).strftime("%a %H:%M"), "Tue 09:21")
        finally:
            system.zone = original

    def test_sync_retries_then_records_state(self):
        calls = []

        def flaky(*args, **kw):
            calls.append(args[0])
            ok = len(calls) > 1  # first call fails like a network that isn't up yet
            return subprocess.CompletedProcess(args, 0 if ok else 1, "", "" if ok else "network unreachable")
        original = cloud._railway
        cloud._railway = flaky
        try:
            cfg = dict(CFG, cloud={"project": "p", "environment": "e", "service": "s"})
            (config.DATA_DIR / "cloud_state.json").unlink(missing_ok=True)
            msg = cloud.sync(cfg, self.SCHED, sleep=lambda s: None)
            self.assertTrue(msg.startswith("cloud schedule updated"), msg)
            self.assertEqual(calls, ["api", "api", "variable"])
            self.assertEqual(cloud.sync(cfg, self.SCHED, sleep=lambda s: None), "cloud schedule unchanged")
        finally:
            cloud._railway = original

    def test_pull_results_reads_railway_structured_logs(self):
        line = json.dumps({"timestamp": "2026-10-01T05:25:59Z", "primer": "inside-window", "at": 1790832355.9,
                           "five_util": 0.99, "five_resets_at": 1790843400, "week_util": 0.7,
                           "week_resets_at": 1791140400, "level": "info", "message": ""})
        noise = json.dumps({"message": '{"primer": "not-due", "at": 1}'})
        original = cloud._railway
        cloud._railway = lambda *a, **k: subprocess.CompletedProcess(a, 0, line + "\n" + noise + "\n", "")
        try:
            db = store.connect(":memory:")
            cfg = dict(CFG, cloud={"project": "p", "environment": "e", "service": "s"})
            self.assertEqual(cloud.pull_results(cfg, db), 1)
            self.assertEqual(cloud.pull_results(cfg, db), 0)
            self.assertEqual(db.execute("select outcome from pings").fetchone()[0], "inside-window")
            db.close()
        finally:
            cloud._railway = original


class Backends(unittest.TestCase):
    SCHED = {"Mon": ["10:51", "17:31"], "Sat": ["10:51"], "Sun": ["17:31"], "Tue": []}

    def test_launchd_plist(self):
        import plistlib
        from primer.backends import launchd
        job = plistlib.loads(launchd.ping_plist(self.SCHED))
        self.assertEqual(job["ProgramArguments"][:2], [str(system.python()), str(system.ENTRY)])
        self.assertEqual(job["ProgramArguments"][2:], ["ping", "--scheduled"])
        self.assertIn({"Weekday": 1, "Hour": 10, "Minute": 51}, job["StartCalendarInterval"])  # Monday
        self.assertIn({"Weekday": 0, "Hour": 17, "Minute": 31}, job["StartCalendarInterval"])  # Sunday
        self.assertEqual(len(job["StartCalendarInterval"]), 4)
        self.assertIn("PATH", job["EnvironmentVariables"])
        self.assertFalse(job["RunAtLoad"])
        replan = plistlib.loads(launchd.replan_plist())
        self.assertEqual(replan["StartCalendarInterval"], [{"Hour": 4, "Minute": 5}])

    def test_task_scheduler_xml(self):
        import xml.etree.ElementTree as ET
        from primer.backends import taskscheduler
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(taskscheduler.ping_xml(self.SCHED, wake=True).encode("utf-16"))
        triggers = root.findall(".//t:CalendarTrigger", ns)
        by_time = {t.find("t:StartBoundary", ns).text[-8:]: sorted(c.tag.split("}")[1] for c in
                   t.find(".//t:DaysOfWeek", ns)) for t in triggers}
        self.assertEqual(by_time, {"10:51:00": ["Monday", "Saturday"], "17:31:00": ["Monday", "Sunday"]})
        self.assertEqual(root.find(".//t:StartWhenAvailable", ns).text, "false")
        self.assertEqual(root.find(".//t:WakeToRun", ns).text, "true")
        self.assertEqual(root.find(".//t:LogonType", ns).text, "InteractiveToken")
        self.assertIn(str(system.ENTRY), root.find(".//t:Arguments", ns).text)
        self.assertTrue(root.find(".//t:Arguments", ns).text.endswith("ping --scheduled"))
        replan = ET.fromstring(taskscheduler.replan_xml().encode("utf-16"))
        self.assertEqual(replan.find(".//t:StartWhenAvailable", ns).text, "true")
        self.assertIsNotNone(replan.find(".//t:ScheduleByDay", ns))

    def test_systemd_service_runs_pinned_interpreter(self):
        from primer.backends import systemd
        unit = systemd._service("x", "ping --scheduled")
        line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        self.assertIn(str(system.ENTRY), line)
        self.assertTrue(line.endswith("ping --scheduled"))
        self.assertIn(str(system.python()), line)  # the pinned interpreter (stable python3 name if it's the same binary)

    def test_launchd_reloads_only_on_change(self):
        from primer.backends import launchd
        calls = []
        original = (launchd.AGENTS, launchd.launchctl, launchd.os.getuid if hasattr(launchd.os, "getuid") else None)
        launchd.AGENTS = Path(tempfile.mkdtemp())
        launchd.launchctl = lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", "")
        if not hasattr(launchd.os, "getuid"):
            launchd.os.getuid = lambda: 501
        try:
            launchd.install(self.SCHED)
            self.assertEqual([c[0] for c in calls], ["bootout", "bootstrap", "bootout", "bootstrap"])
            calls.clear()
            self.assertEqual(launchd.sync(self.SCHED), "ping job unchanged")
            self.assertEqual(calls, [])
            self.assertEqual(launchd.sync({"Mon": []}), "ping job removed (no pings planned)")
            self.assertFalse((launchd.AGENTS / f"{launchd.PING}.plist").exists())
        finally:
            launchd.AGENTS, launchd.launchctl = original[0], original[1]
            if original[2] is None:
                del launchd.os.getuid

    def test_facade_picks_this_os(self):
        from primer import scheduler
        expected = {"linux": "systemd", "macos": "launchd", "windows": "Task Scheduler"}.get(system.OS)
        self.assertEqual(getattr(scheduler.backend(), "NAME", None), expected)


class Portability(unittest.TestCase):
    def setUp(self):
        self._os = system.OS

    def tearDown(self):
        system.OS = self._os

    def test_windows_zone_names_map_to_iana(self):
        system.OS = "windows"
        original_run, saved_tz = system.subprocess.run, os.environ.pop("TZ", None)
        system.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "Pacific Standard Time\r\n", "")
        try:
            self.assertEqual(system.local_tz_name(), "America/Los_Angeles")
            system.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, "W. Europe Standard Time_dstoff", "")
            self.assertEqual(system.local_tz_name(), "Europe/Berlin")
        finally:
            system.subprocess.run = original_run
            if saved_tz is not None:
                os.environ["TZ"] = saved_tz

    def test_npm_cmd_wrapper_resolves_to_real_exe(self):
        system.OS = "windows"
        d = Path(tempfile.mkdtemp())
        shim = d / "claude.cmd"
        shim.write_text("@echo off")
        real = d / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        real.parent.mkdir(parents=True)
        real.write_text("")
        original = system.shutil.which
        system.shutil.which = lambda name: str(shim)
        try:
            self.assertEqual(system.find_program("claude"), str(real))
        finally:
            system.shutil.which = original

    def test_file_lock_is_exclusive(self):
        path = Path(tempfile.mkdtemp()) / "lock"
        with system.file_lock(path):
            with self.assertRaises(BlockingIOError):
                with system.file_lock(path):
                    pass
        with system.file_lock(path):  # released
            pass

    def test_launchers(self):
        files = system.launcher_files()
        for path, content in files.items():
            self.assertIn(system.LAUNCHER_MARK, content)
            # cmd.exe gets native paths; sh scripts (incl. Git Bash on Windows) get forward slashes
            native = path.suffix == ".cmd" or system.OS != "windows"
            self.assertIn(str(system.ENTRY) if native else Path(system.ENTRY).as_posix(), content)
        system.OS = "windows"
        names = sorted(p.name for p in system.launcher_files())
        self.assertEqual(names, ["primer", "primer.cmd"])

    def test_pins_stable_python_name_when_it_is_the_same_interpreter(self):
        if system.OS == "windows":
            self.skipTest("POSIX naming")
        d = Path(tempfile.mkdtemp())
        versioned = d / "python3.99"
        versioned.write_text("")
        (d / "python3").symlink_to(versioned)
        original_exe, original_which = sys.executable, system.shutil.which
        sys.executable = str(versioned)
        try:
            system.shutil.which = lambda name: str(d / "python3")
            self.assertEqual(system.python(), d / "python3")
            system.shutil.which = lambda name: "/somewhere/else/python3"  # a different interpreter
            self.assertEqual(system.python(), versioned)
        finally:
            sys.executable, system.shutil.which = original_exe, original_which

    def test_data_dir_per_os(self):
        saved = os.environ.pop("PRIMER_DATA_DIR")
        try:
            system.OS = "macos"
            self.assertTrue(str(system.data_dir("x")).endswith(os.path.join("Library", "Application Support", "x")))
            system.OS = "windows"
            self.assertTrue(str(system.data_dir("x")).endswith("x"))
            self.assertIn("Local", str(system.data_dir("x")))
        finally:
            os.environ["PRIMER_DATA_DIR"] = saved


if __name__ == "__main__":
    unittest.main()
