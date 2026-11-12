from datetime import date
import json

from helpers import LogTestCase
from ai_costs import claude
from ai_costs.dates import Period


def assistant(message_id="msg-1", model="claude-opus-5-5", timestamp=None, **usage):
    record = dict(type="assistant", requestId="req-1", message=dict(
        id=message_id, model=model, usage={**dict(input_tokens=100, output_tokens=10), **usage}))
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


def dated(timestamp, message_id, output=10):
    return assistant(message_id, timestamp=timestamp, cache_read_input_tokens=200, output_tokens=output,
                     cache_creation_input_tokens=50, cache_creation=dict(ephemeral_5m_input_tokens=50),
                     output_tokens_details=dict(thinking_tokens=5))


class ClaudeTest(LogTestCase):
    def analyze(self, start=None, end=None):
        return claude.analyze([self.path], Period(start, end))

    def test_cache_prices_and_streaming_updates_across_files(self):
        early = assistant(cache_read_input_tokens=1000, cache_creation_input_tokens=300,
                          cache_creation=dict(ephemeral_5m_input_tokens=200, ephemeral_1h_input_tokens=100))
        final = json.loads(json.dumps(early))
        final["message"]["usage"].update(output_tokens=200, output_tokens_details=dict(thinking_tokens=80))
        # One request can yield several content blocks and be copied into another session.
        self.write("project/a.jsonl", [early, final, final])
        self.write("project/subagents/b.jsonl", [final, early])
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"], r["updated_snapshots"]), (1, 4, 1))
        t = r["totals"]
        self.assertEqual((t["cached_input_tokens"], t["output_tokens"], t["reasoning_tokens"]), (1000, 200, 80))
        self.assertAlmostEqual(t["cost_usd"], .0064)

    def test_zero_streaming_placeholders_require_valid_snapshot(self):
        partial = assistant()
        partial["message"]["usage"].update(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
            cache_creation_input_tokens=0, cache_creation=dict(ephemeral_1h_input_tokens=100),
            output_tokens_details=dict(thinking_tokens=5))
        complete = assistant(cache_creation_input_tokens=100, cache_creation=dict(ephemeral_1h_input_tokens=100))
        self.write("a.jsonl", [partial, complete])
        self.write("b.jsonl", [partial])
        r = self.analyze()
        self.assertEqual((r["invalid_records"], r["recovered_partial_snapshots"], r["usage_events"]), (0, 2, 1))
        self.assertEqual(r["totals"]["total_tokens"], 210)
        partial["message"]["id"] = "orphan"
        self.write("c.jsonl", [partial])
        r = self.analyze()
        self.assertEqual((r["invalid_records"], r["totals"]["total_tokens"]), (1, 210))

    def test_model_ids_with_date_and_context_suffix(self):
        models = ["claude-opus-5-5[1m]", "claude-sonnet-5-5", "claude-sonnet-4-6",
                  "claude-haiku-4-5-20251001", "claude-fable-5-2"]
        self.write("models.jsonl", [assistant(str(i), m) for i, m in enumerate(models)])
        r = self.analyze()
        self.assertEqual(r["usage_events"], 5)
        self.assertTrue(r["totals"]["pricing_complete"])
        self.assertAlmostEqual(r["totals"]["cost_usd"], .0027)

    def test_invalid_records_synthetic_and_conversation_content(self):
        invalid = assistant("bad", cache_creation_input_tokens=5, cache_creation=dict(ephemeral_5m_input_tokens=6))
        negative = assistant("negative")
        negative["message"]["usage"]["input_tokens"] = -1
        bad_type = assistant("bool")
        bad_type["message"]["usage"]["input_tokens"] = True
        reasoning = assistant("reasoning", output_tokens_details=dict(thinking_tokens=11))
        path = self.write("bad.jsonl", [assistant(), invalid, negative, bad_type, reasoning,
            assistant("synthetic", "<synthetic>"), {"type": "user", "message": assistant()},
            {"type": "assistant", "message": "broken"}, 42])
        with path.open("a") as stream:
            stream.write("{broken\n\n")
        (self.path / "broken.json").write_text('[{"type":"assistant"},')
        (self.path / "ignored.txt").write_text("not JSON")
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["invalid_records"], r["files_failed"]), (1, 7, 1))
        self.assertEqual(r["skipped_synthetic_records"], 1)

    def test_unknown_model_and_missing_ttl(self):
        self.write("unknown.jsonl", [assistant("unknown", "future-model"), assistant("known", cache_creation_input_tokens=100)])
        r = self.analyze()
        self.assertIsNone(r["totals"]["cost_usd"])
        self.assertFalse(r["totals"]["pricing_complete"])
        self.assertAlmostEqual(r["totals"]["known_cost_usd"], .0011)
        self.assertEqual(r["assumed_5m_cache_events"], 1)

    def test_json_containers_uuid_fallback_and_missing_ids(self):
        a = assistant(None)
        a["uuid"] = "unique"
        b = assistant(None)
        (self.path / "export.json").write_text(json.dumps({"records": [{"events": [a, a, b, b]}]}))
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"]), (3, 1))

    def test_deduplicates_before_date_filter(self):
        self.write("claude.jsonl", [
            dated("2026-11-01T12:00:00Z", "old"),
            dated("2026-11-02T00:00:00+01:00", "offset-old"),
            dated("2026-11-02T00:00:00Z", "first"),
            dated("2026-11-02T23:59:59.999999999Z", "last"),
            dated("2026-11-03T00:30:00+01:00", "offset-inside"),
            dated("2026-11-02T23:59:58Z", "stream"),
            dated("2026-11-03T00:00:01Z", "stream", 100),
            dated(None, "undated"), dated("invalid", "invalid"),
        ])
        r = self.analyze(date(2026, 11, 2), date(2026, 11, 2))
        self.assertEqual((r["usage_events"], r["totals"]["total_tokens"], r["totals"]["reasoning_tokens"]), (3, 1080, 15))
        self.assertEqual((r["filtered_usage_events"], r["skipped_undated_usage_events"], r["duplicate_snapshots"]), (3, 2, 1))
