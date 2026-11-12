"""Token usage from Claude Code transcripts (~/.claude/projects/**/*.jsonl).

Claude Code writes one assistant record per content block and copies history
into subagent and resumed sessions, so usage is deduplicated by message ID
across all files before it is counted.
"""
import sys

from . import logs
from .dates import local_day, parse_timestamp
from .pricing import CLAUDE_PRICING, claude_prices
from .report import TOKEN_FIELDS, finalize, new_report, zero

_MAIN_COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid token count")
    return value


def normalize(usage):
    """Return (token counts, whether a missing cache TTL was assumed to be 5 minutes)."""
    if not isinstance(usage, dict) or not {"input_tokens", "output_tokens"} <= usage.keys():
        raise ValueError("missing usage")
    creation = _count(usage.get("cache_creation_input_tokens", 0))
    details = usage.get("cache_creation")
    assumed = details is None and creation > 0
    if details is None:
        five, hour = creation, 0  # Older logs omit the TTL; 5 minutes is the default.
    else:
        five = _count(details.get("ephemeral_5m_input_tokens", 0))
        hour = _count(details.get("ephemeral_1h_input_tokens", 0))
        if five + hour != creation:
            raise ValueError("cache totals disagree")
    output = _count(usage["output_tokens"])
    reasoning = _count((usage.get("output_tokens_details") or {}).get("thinking_tokens", 0))
    if reasoning > output:
        raise ValueError("reasoning exceeds output")
    counts = (_count(usage["input_tokens"]), _count(usage.get("cache_read_input_tokens", 0)),
              five, hour, output, reasoning)
    return dict(zip(TOKEN_FIELDS, counts)), assumed


def _is_placeholder(usage, message_id):
    # Streaming can write zero main counters while cache details or thinking are
    # already filled. Such a record is only acceptable if a valid snapshot of the
    # same message exists somewhere; that is checked after all files are read.
    return (isinstance(usage, dict) and isinstance(message_id, str) and message_id
            and all(type(usage.get(k)) is int and usage[k] == 0 for k in _MAIN_COUNTERS))


def _rank(snapshot):
    # Output grows while streaming, input is repeated per content block:
    # keep the most complete snapshot of each message.
    usage, moment = snapshot[1], snapshot[3]
    return (usage["output_tokens"], sum(usage[k] for k in TOKEN_FIELDS[:4]),
            usage["reasoning_tokens"], moment is not None)


def analyze(paths, period, group="model"):
    report = new_report("claude", paths, period, CLAUDE_PRICING, duplicate_snapshots=0,
                        updated_snapshots=0, recovered_partial_snapshots=0,
                        assumed_5m_cache_events=0, skipped_synthetic_records=0)
    snapshots, placeholders = {}, {}
    for file in logs.find_files(paths):
        report["files_scanned"] += 1
        try:
            for index, value in logs.read_values(file):
                if value is logs.INVALID:
                    report["invalid_records"] += 1
                    continue
                for ordinal, record in enumerate(logs.records(value)):
                    _read_record(record, report, snapshots, placeholders, (str(file), index, ordinal))
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            report["files_failed"] += 1
            print(f"ai-costs: warning: claude: skipped {file} ({type(error).__name__})", file=sys.stderr)
    for message_id, count in placeholders.items():
        key = "recovered_partial_snapshots" if ("message", message_id) in snapshots else "invalid_records"
        report[key] += count

    totals = {}
    for model, usage, assumed, moment in snapshots.values():
        day = local_day(moment) if moment else None
        if period.active:
            if day is None:
                report["skipped_undated_usage_events"] += 1
                continue
            if not period.contains(day):
                report["filtered_usage_events"] += 1
                continue
        report["usage_events"] += 1
        report["assumed_5m_cache_events"] += int(assumed)
        row = totals.setdefault((model, day), zero())
        for field in TOKEN_FIELDS:
            row[field] += usage[field]
    return finalize(report, totals, claude_prices, group)


def _read_record(record, report, snapshots, placeholders, position):
    if not isinstance(record, dict):
        report["invalid_records"] += 1
        return
    if record.get("type") != "assistant":
        return
    try:
        message = record["message"]
        if message.get("model") == "<synthetic>":  # Locally generated error messages.
            report["skipped_synthetic_records"] += 1
            return
        if message.get("usage") is None:
            return
        try:
            usage, assumed = normalize(message["usage"])
        except ValueError:
            if _is_placeholder(message["usage"], message.get("id")):
                placeholders[message["id"]] = placeholders.get(message["id"], 0) + 1
                return
            raise
        model = message.get("model") or "unknown"
        if not isinstance(model, str):
            raise ValueError("invalid model")
        message_id, uuid = message.get("id"), record.get("uuid")
        if isinstance(message_id, str) and message_id:
            key = ("message", message_id)
        elif isinstance(uuid, str) and uuid:
            key = ("uuid", uuid)
        else:
            key = ("position", *position)
        candidate = (model, usage, assumed, parse_timestamp(record.get("timestamp")))
        previous = snapshots.get(key)
        if previous is not None:
            report["duplicate_snapshots"] += 1
            if _rank(candidate) <= _rank(previous):
                return
            report["updated_snapshots"] += 1
        snapshots[key] = candidate
    except (KeyError, TypeError, ValueError, AttributeError):
        report["invalid_records"] += 1
