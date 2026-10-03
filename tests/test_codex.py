from datetime import date

from helpers import LogTestCase
from ai_costs import codex
from ai_costs.dates import Period


def token_count(timestamp, input_tokens, output_tokens, last=None):
    info = dict(total_token_usage=dict(input_tokens=input_tokens, output_tokens=output_tokens))
    if last:
        info["last_token_usage"] = last
    return dict(timestamp=timestamp, type="event_msg", payload=dict(type="token_count", info=info))


def usage(i, c, o, r):
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o, reasoning_output_tokens=r)


class CodexTest(LogTestCase):
    def analyze(self, path=None, start=None, end=None):
        return codex.analyze([path or self.path], Period(start, end))

    def test_schema_variants_model_switch_duplicates_and_resets(self):
        self.write("nested directory/session.jsonl", [
            dict(type="turn_context", payload=dict(model="gpt-6-sol")),
            dict(type="token_usage_record", payload=dict(usage=usage(1000, 400, 100, 20), thread_token_usage=usage(1000, 400, 100, 20))),
            dict(type="event_msg", payload=dict(type="token_count", info=dict(total_token_usage=usage(1000, 400, 100, 20), last_token_usage=usage(1000, 400, 100, 20)))),
            dict(type="event_msg", payload=dict(type="token_count", info=dict(total_token_usage=usage(1000, 400, 100, 20)))),
            dict(type="turn_context", payload=dict(model="gpt-6.1-sol")),
            dict(type="event_msg", payload=dict(type="token_count", info=dict(total_token_usage=usage(1500, 600, 150, 30)))),
            dict(type="token_usage_record", payload=dict(usage=usage(500, 200, 50, 10), thread_token_usage=usage(1500, 600, 150, 30))),
            dict(type="event_msg", payload=dict(type="token_count", info=dict(total_token_usage=usage(100, 20, 10, 2), last_token_usage=usage(100, 20, 10, 2)))),
            dict(payload="unexpected string"),
            dict(usage=dict(prompt_tokens_details="unexpected string")),
            dict(model="gpt-6-luna", usage=dict(prompt_tokens=10, cached_tokens=11, completion_tokens=1)),
            dict(type="event_msg", payload=dict(type="token_count", info=None)),
        ])
        with (self.path / "nested directory/session.jsonl").open("a") as stream:
            stream.write("this is broken JSON\n")
        (self.path / "api.json").write_text("""[
          {"model":"gpt-6-luna","usage":{"prompt_tokens":1000,"prompt_tokens_details":{"cached_tokens":500},"completion_tokens":100,"completion_tokens_details":{"reasoning_tokens":50}}},
          {"model":"gpt-6-luna","usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":50},"output_tokens":10,"output_tokens_details":{"reasoning_tokens":5}}}
        ]""")
        (self.path / "broken.json").write_text("{broken")
        (self.path / "not-a-log.txt").write_text("ignored")
        r = self.analyze()
        self.assertEqual((r["files_scanned"], r["files_failed"], r["invalid_records"]), (3, 1, 4))
        self.assertEqual((r["duplicate_snapshots"], r["counter_resets"], r["usage_events"]), (1, 1, 5))
        t = r["totals"]
        self.assertEqual((t["uncached_input_tokens"], t["cached_input_tokens"], t["output_tokens"], t["reasoning_tokens"]),
                         (1530, 1170, 270, 87))
        self.assertTrue(t["pricing_complete"])
        self.assertAlmostEqual(t["cost_usd"], 0.0037775)
        sol = next(m for m in r["models"] if m["model"] == "gpt-6.1-sol")
        self.assertEqual((sol["uncached_input_tokens"], sol["cached_input_tokens"], sol["output_tokens"]), (380, 220, 60))

    def test_empty_and_unknown_model(self):
        empty = self.path / "empty"
        empty.mkdir()
        r = self.analyze(empty)
        self.assertEqual((r["files_scanned"], r["models"], r["totals"]["cost_usd"]), (0, [], 0))
        path = self.write("unknown.jsonl", [dict(usage=dict(prompt_tokens=100, completion_tokens=10, reasoning_tokens=2))])
        r = self.analyze(path)
        self.assertEqual(r["models"][0]["model"], "unknown")
        self.assertIsNone(r["totals"]["cost_usd"])
        self.assertEqual(r["totals"]["known_cost_usd"], 0)
        self.assertFalse(r["totals"]["pricing_complete"])

    def test_token_usage_record_without_token_count(self):
        path = self.write("standalone.jsonl", [
            dict(type="turn_context", payload=dict(model="gpt-6-sol")),
            dict(type="token_usage_record", payload=dict(usage=dict(input_tokens=100, output_tokens=10), thread_token_usage=dict(input_tokens=100, output_tokens=10))),
            dict(type="token_usage_record", payload=dict(usage=dict(input_tokens=50, output_tokens=5), thread_token_usage=dict(input_tokens=150, output_tokens=15))),
        ])
        r = self.analyze(path)
        self.assertEqual((r["totals"]["uncached_input_tokens"], r["totals"]["output_tokens"]), (150, 15))

    def test_date_filter_keeps_baseline_and_handles_offsets(self):
        path = self.write("codex.jsonl", [
            dict(type="turn_context", payload=dict(model="gpt-6-sol")),
            token_count("2026-11-01T12:00:00Z", 1000, 100),
            token_count("2026-11-02T00:00:00+01:00", 1200, 120),
            dict(type="turn_context", payload=dict(model="gpt-6.1-sol")),
            token_count("2026-11-02T00:00:00Z", 1500, 150),
            token_count("2026-11-02T23:59:59.999999999Z", 1600, 160),
            token_count("2026-11-03T00:30:00+01:00", 1800, 180),
            token_count("2026-11-03T00:00:00Z", 2000, 200),
            token_count(None, 2100, 210),
            token_count("invalid", 2200, 220),
        ])
        r = self.analyze(path, date(2026, 11, 2), date(2026, 11, 2))
        self.assertEqual(r["usage_events"], 3)
        self.assertEqual(r["totals"]["total_tokens"], 660)
        self.assertEqual(r["totals"]["uncached_input_tokens"], 600)
        self.assertEqual(r["skipped_undated_usage_events"], 2)
        self.assertEqual(r["filtered_usage_events"], 3)
        self.assertEqual(r["models"][0]["model"], "gpt-6.1-sol")

    def test_filtered_reset_and_open_bounds(self):
        reset = token_count("2026-11-02T12:00:00Z", 50, 5, dict(input_tokens=20, output_tokens=2))
        path = self.write("reset.jsonl", [
            dict(type="turn_context", payload=dict(model="gpt-6-sol")),
            token_count("2026-11-01T12:00:00Z", 1000, 100), reset,
            dict(timestamp="2026-11-02T12:00:00Z", model="gpt-6-sol", usage=dict(input_tokens=10, output_tokens=1))])
        r = self.analyze(path, date(2026, 11, 2))
        self.assertEqual((r["counter_resets"], r["totals"]["total_tokens"]), (1, 33))
        path = self.write("fallback.jsonl", [dict(timestamp="2026-11-02T12:00:00Z", type="token_usage_record",
                                                   payload=dict(usage=dict(input_tokens=10, output_tokens=1)))])
        self.assertEqual(self.analyze(path, None, date(2026, 11, 2))["totals"]["total_tokens"], 11)

    def test_invalid_timestamps_do_not_reset_state(self):
        rows = [dict(model="gpt-6-sol", timestamp=t, usage=dict(input_tokens=10, output_tokens=1))
                for t in ("bad", "2026-02-30T00:00:00Z", "2026-11-02T12:00:00+00:60", "2026-11-02T12:00:00Z")]
        r = self.analyze(self.write("t.jsonl", rows), date(2026, 11, 2))
        self.assertEqual((r["usage_events"], r["skipped_undated_usage_events"]), (1, 3))
