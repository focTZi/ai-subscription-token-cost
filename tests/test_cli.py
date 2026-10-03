from datetime import date, datetime, timedelta, timezone
import io
import json
import os

from helpers import LogTestCase
from ai_costs import cli, dates
from test_claude import dated
from test_codex import token_count


class PeriodTest(LogTestCase):
    def test_presets(self):
        today = date(2026, 3, 15)
        self.assertEqual(dates.parse_period("30d", today).start, date(2026, 2, 14))
        self.assertEqual(dates.parse_period("1d", today).start, today)
        p = dates.parse_period("last-month", today)
        self.assertEqual((p.start, p.end), (date(2026, 2, 1), date(2026, 2, 28)))
        p = dates.parse_period("this-month", today)
        self.assertEqual((p.start, p.end), (date(2026, 3, 1), today))
        p = dates.parse_period("2025-12", today)
        self.assertEqual((p.start, p.end), (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertFalse(dates.parse_period("all", today).active)
        for bad in ("0d", "2026-13", "yesterday", ""):
            with self.assertRaises(ValueError):
                dates.parse_period(bad, today)

    def test_dates_and_timestamps(self):
        for bad in ("2026-02-30", "2026-1-02", ""):
            with self.assertRaises(ValueError):
                dates.parse_date(bad)
        with self.assertRaises(ValueError):
            dates.Period(date(2026, 11, 3), date(2026, 11, 2))
        self.assertEqual(dates.parse_timestamp("2026-11-02T00:30:00+01:00").date(), date(2026, 11, 1))
        self.assertEqual(dates.parse_timestamp("2026-11-02T23:59:59.999999999Z").date(), date(2026, 11, 2))
        for bad in (None, "2026-11-02", "2026-11-02T12:00:00", "2026-11-02T12:00:00+00:60", "2026-11-02T24:00:00Z"):
            self.assertIsNone(dates.parse_timestamp(bad))


class InteractiveTest(LogTestCase):
    def test_menus_use_defaults_and_retry(self):
        out = io.StringIO()
        self.assertEqual(cli.ask_provider("both", io.StringIO("\n"), out), "both")
        self.assertEqual(cli.ask_provider("both", io.StringIO("x\n9\n3\n"), out), "claude")
        self.assertIn("between 1 and 3", out.getvalue())
        today = date(2026, 11, 12)
        p = cli.ask_period(today, io.StringIO("\n"), out)
        self.assertEqual((p.start, p.end), (date(2026, 10, 14), today))
        p = cli.ask_period(today, io.StringIO("6\n2026-11-31\n2026-11-01\n\n"), out)
        self.assertEqual((p.start, p.end), (date(2026, 11, 1), today))
        with self.assertRaises(KeyboardInterrupt):
            cli.ask_provider("both", io.StringIO(""), out)


class CommandLineTest(LogTestCase):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, HOME=str(self.path))
        self.env.pop("CODEX_HOME", None)
        self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.today = datetime.now(timezone.utc).date()
        self.start = self.today - timedelta(days=29)
        old = self.start - timedelta(days=1)
        self.write(".codex/sessions/codex.jsonl", [dict(type="turn_context", payload=dict(model="gpt-6-sol")),
            token_count(f"{old}T12:00:00Z", 1000, 100), token_count(f"{self.start}T00:00:00Z", 2000, 200)])
        self.write(".codex/archived_sessions/archived.jsonl", [dict(model="gpt-6-sol", timestamp=f"{self.today}T12:00:00Z",
            usage=dict(input_tokens=10, output_tokens=1))])
        self.write(".claude/projects/claude.jsonl", [dated(f"{old}T12:00:00Z", "old"), dated(f"{self.today}T12:00:00Z", "today")])

    def test_defaults_last_30_days_both_providers(self):
        r = json.loads(self.run_cli("--json", env=self.env, check=True).stdout)
        self.assertEqual(r["period"]["from_date"], str(self.start))
        self.assertEqual(r["providers"]["codex"]["totals"]["total_tokens"], 1111)
        self.assertEqual(r["providers"]["claude"]["totals"]["total_tokens"], 360)
        self.assertEqual(r["totals"]["total_tokens"], 1471)
        self.assertAlmostEqual(r["totals"]["cost_usd"], .00389 + .00003)
        table = self.run_cli(env=self.env, check=True).stdout
        for text in ("last 30 days", "Codex", "Claude", "1.47K", "Total"):
            self.assertIn(text, table)

    def test_provider_period_and_paths(self):
        r = json.loads(self.run_cli("--json", "-p", "claude", "--period", "all", env=self.env, check=True).stdout)
        self.assertEqual(list(r["providers"]), ["claude"])
        self.assertEqual(r["totals"]["total_tokens"], 720)
        r = json.loads(self.run_cli("--json", "-p", "codex", "--from", str(self.today),
                                    "--codex-path", self.path / ".codex/sessions", env=self.env, check=True).stdout)
        self.assertEqual(r["totals"]["total_tokens"], 0)
        self.assertEqual(r["period"]["to_date"], None)

    def test_only_installed_provider_is_default(self):
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.path / "nothing"))
        r = json.loads(self.run_cli("--json", env=env, check=True).stdout)
        self.assertEqual(list(r["providers"]), ["codex"])

    def test_unknown_price_marks_totals_incomplete(self):
        self.write(".codex/sessions/unknown.jsonl", [dict(model="future-model", timestamp=f"{self.today}T12:00:00Z",
                                                          usage=dict(input_tokens=10, output_tokens=1))])
        result = self.run_cli("--json", env=self.env, check=True)
        r = json.loads(result.stdout)
        self.assertIsNone(r["totals"]["cost_usd"])
        self.assertFalse(r["totals"]["pricing_complete"])
        self.assertIn("no price for future-model", result.stderr)
        table = self.run_cli(env=self.env, check=True).stdout
        self.assertIn("No price for: Codex future-model (11 tokens)", table)
        self.assertIn("n/a", table)

    def test_argument_errors(self):
        for args in (("--period", "soon"), ("--from", "2026-02-30"), ("--from", "2026-11-03", "--to", "2026-11-02"),
                     ("--period", "7d", "--to", "2026-11-02"), ("--period", "7d", "--from", "2026-11-01"),
                     ("--codex-path", self.path / "missing", "-p", "codex"), ("--invalid",)):
            result = self.run_cli("--json", *args, env=self.env)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, "")
