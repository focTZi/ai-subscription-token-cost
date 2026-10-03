"""Token usage from Codex CLI session logs (rollout-*.jsonl).

Codex writes cumulative counters (event_msg/token_count with total_token_usage).
Usage is counted as the difference between consecutive totals per file, so a
resumed or repeated snapshot is never counted twice.
"""
import sys

from . import logs
from .dates import parse_timestamp
from .pricing import CODEX_PRICING, codex_prices
from .report import finalize, new_report, zero

FIELDS = ("input", "cached", "output", "reasoning")
# Alternatives are tried in order, like jq's `a // b`: the first non-null value wins.
ALTERNATIVES = dict(
    input=(("input_tokens",), ("prompt_tokens",)),
    cached=(("cached_input_tokens",), ("cached_tokens",),
            ("prompt_tokens_details", "cached_tokens"), ("input_tokens_details", "cached_tokens")),
    output=(("output_tokens",), ("completion_tokens",)),
    reasoning=(("reasoning_output_tokens",), ("reasoning_tokens",),
               ("completion_tokens_details", "reasoning_tokens"),
               ("output_tokens_details", "reasoning_tokens")),
)


class _Malformed(Exception):
    """A record has an unexpected shape, e.g. a string where an object belongs."""


def _get(value, *keys):
    for key in keys:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise _Malformed
        value = value.get(key)
    return value


def _alternative(value, paths, default=None):
    for path in paths:
        found = _get(value, *path)
        if found is not None and found is not False:
            return found
    return default


def _is_count(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and value >= 0 and value.is_integer()


def normalize(usage):
    """Return {input, cached, output, reasoning} or None if the usage is not plausible."""
    if not isinstance(usage, dict):
        return None
    counts = {field: _alternative(usage, ALTERNATIVES[field], 0) for field in FIELDS}
    if not all(_is_count(v) for v in counts.values()):
        return None
    counts = {k: int(v) for k, v in counts.items()}
    if counts["cached"] > counts["input"] or counts["reasoning"] > counts["output"]:
        return None
    return counts


def _model(record):
    candidates = (
        _get(record, "model"), _get(record, "payload", "model"), _get(record, "payload", "info", "model"),
        _get(record, "payload", "thread_settings", "model"), _get(record, "payload", "turn_context", "model"),
        _get(record, "payload", "base_instructions", "provenance", "model")
        if record.get("type") == "session_meta" else None,
    )
    return next((c for c in candidates if isinstance(c, str) and c), None)


def _is_token_count(record):
    return record.get("type") == "event_msg" and _get(record, "payload", "type") == "token_count"


def _usage(record, prefer_counts):
    """Return (cumulative total, single request usage) for a record."""
    if _is_token_count(record):
        return _get(record, "payload", "info", "total_token_usage"), _get(record, "payload", "info", "last_token_usage")
    if record.get("type") == "token_usage_record":
        if prefer_counts:
            return None, None
        return _get(record, "payload", "thread_token_usage"), _get(record, "payload", "usage")
    return None, _alternative(record, (("usage",), ("payload", "usage")))


def _has_cumulative_counts(file):
    # token_count and token_usage_record are separate counter series that can
    # diverge in resumed sessions. Never mix them: prefer token_count if present.
    try:
        for _, value in logs.read_values(file):
            for record in logs.records(value):
                try:
                    if (isinstance(record, dict) and _is_token_count(record)
                            and normalize(_get(record, "payload", "info", "total_token_usage")) is not None):
                        return True
                except _Malformed:
                    pass
    except (OSError, UnicodeError, ValueError):
        pass
    return False


def _scan(file, prefer_counts, period):
    state = dict(models={}, usage_events=0, invalid_records=0, duplicate_snapshots=0, counter_resets=0,
                 skipped_undated_usage_events=0, filtered_usage_events=0)
    model, previous = "unknown", None

    def add(usage, record):
        if period.active:
            moment = parse_timestamp(record.get("timestamp"))
            if moment is None:
                state["skipped_undated_usage_events"] += 1
                return
            if not period.contains(moment):
                state["filtered_usage_events"] += 1
                return
        state["usage_events"] += 1
        row = state["models"].setdefault(model, zero())
        row["uncached_input_tokens"] += usage["input"] - usage["cached"]
        row["cached_input_tokens"] += usage["cached"]
        row["output_tokens"] += usage["output"]
        row["reasoning_tokens"] += usage["reasoning"]

    for _, value in logs.read_values(file):
        for record in logs.records(value):
            if not isinstance(record, dict):
                state["invalid_records"] += 1
                continue
            # Evaluate everything that can fail first; a malformed record changes no state.
            try:
                found_model = _model(record)
                total, last = _usage(record, prefer_counts)
                if total is not None:
                    current = normalize(total)
                    delta = reset = None
                    if current is not None and current != previous:
                        reset = previous is not None and any(current[f] < previous[f] for f in FIELDS)
                        if previous is None:
                            delta = current
                        elif reset:
                            delta = normalize(last) or current
                        else:
                            delta = {f: current[f] - previous[f] for f in FIELDS}
                elif last is not None:
                    single = normalize(last)
            except _Malformed:
                state["invalid_records"] += 1
                continue
            model = found_model or model
            if total is not None:
                if current is None:
                    state["invalid_records"] += 1
                elif current == previous:
                    state["duplicate_snapshots"] += 1
                else:
                    state["counter_resets"] += int(reset)
                    previous = current
                    if delta["cached"] > delta["input"] or delta["reasoning"] > delta["output"]:
                        state["invalid_records"] += 1
                    else:
                        add(delta, record)
            elif last is not None:
                if single is None:
                    state["invalid_records"] += 1
                else:
                    add(single, record)
    return state


def analyze(paths, period):
    report = new_report("codex", paths, period, CODEX_PRICING, duplicate_snapshots=0, counter_resets=0)
    models = {}
    for file in logs.find_files(paths):
        report["files_scanned"] += 1
        try:
            state = _scan(file, _has_cumulative_counts(file), period)
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            report["files_failed"] += 1
            print(f"ai-costs: warning: codex: skipped {file} ({type(error).__name__})", file=sys.stderr)
            continue
        for name, counts in state.pop("models").items():
            row = models.setdefault(name, zero())
            for field, count in counts.items():
                row[field] += count
        for counter, count in state.items():
            report[counter] += count
    return finalize(report, models, codex_prices)
