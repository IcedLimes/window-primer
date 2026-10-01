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
os.environ["TZ"] = "America/Los_Angeles"
time.tzset()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from primer import cloud, config, ingest, ping, planner, pricing, sim, statusline, store  # noqa: E402

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


class PingRun(unittest.TestCase):
    def setUp(self):
        self.db = store.connect(":memory:")
        self.cfg = dict(CFG, claude_bin="/bin/true", ping_retries=1)

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

    def test_skips_when_window_known_open(self):
        store.add_observation(self.db, time.time(), "statusline", 0.2, time.time() + 3600)
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

    def test_command_disables_hooks_and_tools(self):
        cmd = ping.ping_command(self.cfg)
        self.assertIn("--safe-mode", cmd)
        self.assertIn('{"disableAllHooks":true}', cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")


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


if __name__ == "__main__":
    unittest.main()
