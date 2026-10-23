from datetime import date
import copy

from helpers import LogTestCase
from ai_costs import pi
from ai_costs.dates import Period


def header(session_id, parent=None):
    result = dict(type="session", version=3, id=session_id)
    if parent is not None:
        result["parentSession"] = str(parent)
    return result


def assistant(entry_id="entry", response_id="response", provider="anthropic",
              model="claude-opus-5-5", timestamp="2026-11-02T12:00:00Z", **usage):
    message = dict(role="assistant", provider=provider, model=model,
                   usage=dict(input=100, output=10, cacheRead=200, cacheWrite=50))
    message["usage"].update(usage)
    if response_id is not None:
        message["responseId"] = response_id
    return dict(type="message", id=entry_id, timestamp=timestamp, message=message)


class PiTest(LogTestCase):
    def analyze(self, period=None, group="model"):
        return pi.analyze([self.path], period or Period(), group)

    def test_normalization_prices_and_logged_cost_is_ignored(self):
        self.write("a.jsonl", [header("a"), assistant(cacheWrite1h=20, reasoning=5,
                                                      totalTokens=999, cost=dict(total=999))])
        r = self.analyze()
        row = r["models"][0]
        self.assertEqual(row["model"], "anthropic/claude-opus-5-5")
        self.assertEqual((row["cache_creation_5m_input_tokens"], row["cache_creation_1h_input_tokens"]), (30, 20))
        self.assertEqual((row["total_tokens"], row["reasoning_tokens"]), (360, 5))
        self.assertAlmostEqual(row["cost_usd"], .00095)
        self.assertEqual(r["assumed_5m_cache_events"], 0)

    def test_forks_copies_and_independent_entry_ids(self):
        entry = assistant(response_id=None)
        a = self.write("a.jsonl", [header("a"), entry])
        self.write("copy.jsonl", [header("a"), entry])
        fork = self.write("fork.jsonl", [header("fork", a), entry])
        self.write("fork2.jsonl", [header("fork2", fork), entry])
        self.write("independent.jsonl", [header("independent"), entry])
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"]), (2, 3))
        self.assertEqual(r["totals"]["total_tokens"], 720)

    def test_response_ids_and_all_tree_branches(self):
        early = assistant(output=2)
        final = assistant("rewritten", output=20)
        other = assistant("branch", "other", provider="openai", model="gpt-6-sol", cacheWrite=0)
        self.write("a.jsonl", [header("a"), early, other])
        self.write("b.jsonl", [header("b"), final, final])
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"], r["updated_snapshots"]), (2, 2, 1))
        self.assertEqual(r["totals"]["output_tokens"], 30)
        self.assertEqual(len(r["models"]), 2)

    def test_additional_usage_and_missing_attribution(self):
        usage = dict(input=10, output=2, cacheRead=0, cacheWrite=0)
        entries = [dict(type="usage", id="warm", kind="future-kind", provider="anthropic",
                        model="claude-opus-5-5", usage=usage, timestamp="2026-11-02T12:00:00Z")]
        entries += [dict(type=kind, id=kind, usage=usage) for kind in ("compaction", "branch_summary")]
        entries.append(dict(type="message", id="tool", message=dict(role="toolResult", usage=usage)))
        self.write("a.jsonl", [header("a"), *entries])
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["unattributed_usage_events"]), (4, 3))
        self.assertEqual(r["totals"]["total_tokens"], 48)
        self.assertIsNone(r["totals"]["cost_usd"])

    def test_unknown_provider_does_not_borrow_model_price(self):
        self.write("a.jsonl", [assistant(provider="custom"),
            assistant("known", "known", provider="openai-codex", model="gpt-6-sol", cacheWrite=0),
            assistant("unknown", "unknown", provider="openai", model="future-model", cacheWrite=0)])
        r = self.analyze()
        rows = {row["model"]: row for row in r["models"]}
        self.assertIsNone(rows["custom/claude-opus-5-5"]["cost_usd"])
        self.assertIsNone(rows["openai/future-model"]["cost_usd"])
        self.assertAlmostEqual(rows["openai-codex/gpt-6-sol"]["cost_usd"], .00034)

    def test_unpriced_cache_write_is_not_silently_free(self):
        self.write("a.jsonl", [assistant(provider="openai", model="gpt-6-sol")])
        self.assertIsNone(self.analyze()["totals"]["cost_usd"])

    def test_dates_timestamp_fallback_and_grouping(self):
        fallback = assistant("fallback", "fallback", timestamp=None)
        fallback["message"]["timestamp"] = 1793664000000  # 2026-11-03 UTC
        self.write("a.jsonl", [assistant(), fallback, assistant("undated", "undated", timestamp="bad")])
        r = self.analyze(Period(date(2026, 11, 3), date(2026, 11, 3)), "day")
        self.assertEqual((r["usage_events"], r["filtered_usage_events"], r["skipped_undated_usage_events"]), (1, 1, 1))
        self.assertEqual([g["key"] for g in r["groups"]], ["2026-11-03"])
        for group in ("day", "week", "month"):
            all_time = self.analyze(group=group)
            self.assertEqual(sum(g["totals"]["total_tokens"] for g in all_time["groups"]), 1080)
            self.assertIn("undated", [g["key"] for g in all_time["groups"]])

    def test_invalid_records_and_aborted_usage(self):
        rows = [header("a"), 42, dict(type="message", message="broken")]
        for bad in (dict(input=-1), dict(output=True), dict(cacheRead=1.5), dict(reasoning=11), dict(cacheWrite1h=51)):
            rows.append(assistant(**bad))
        aborted = assistant()
        aborted["message"]["stopReason"] = "aborted"
        rows += [aborted, dict(type="model_change", modelId="ignore"), dict(type="message", message=dict(role="user"))]
        path = self.write("a.jsonl", rows)
        with path.open("a") as stream:
            stream.write("{broken\n")
        (self.path / "broken.json").write_text("[{")
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["invalid_records"], r["files_failed"]), (1, 8, 1))
        self.assertEqual(r["assumed_5m_cache_events"], 1)

    def test_missing_ids_are_not_deduplicated_and_zero_errors_ignored(self):
        record = assistant(response_id=None)
        record.pop("id")
        empty = assistant(input=0, output=0, cacheRead=0, cacheWrite=0)
        self.write("a.jsonl", [record, copy.deepcopy(record), empty])
        r = self.analyze()
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"]), (2, 0))

    def test_deduplication_precedes_date_filter(self):
        early = assistant(timestamp="2026-11-02T23:59:59Z", output=1)
        final = assistant(timestamp="2026-11-03T00:00:01Z", output=20)
        self.write("a.jsonl", [early, final])
        r = self.analyze(Period(date(2026, 11, 2), date(2026, 11, 2)))
        self.assertEqual((r["usage_events"], r["duplicate_snapshots"], r["filtered_usage_events"]), (0, 1, 1))
