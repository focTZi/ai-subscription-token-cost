"""Token usage from Pi session trees, including copied fork history.

Only usage and identifying metadata are retained; conversation content is ignored.
"""
from datetime import datetime, timezone
from pathlib import Path
import sys

from . import logs
from .dates import local_day, parse_timestamp
from .pricing import PI_PRICING, pi_prices
from .report import BILLED_FIELDS, TOKEN_FIELDS, finalize, new_report, zero


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError("invalid token count")
    return value


def normalize(usage):
    if not isinstance(usage, dict):
        raise ValueError("missing usage")
    counts = [_count(usage[k]) for k in ("input", "cacheRead", "cacheWrite", "output")]
    hour = _count(usage.get("cacheWrite1h", 0))
    reasoning = _count(usage.get("reasoning", 0))
    if hour > counts[2] or reasoning > counts[3]:
        raise ValueError("invalid cache or reasoning subset")
    # Recompute totals: providers do not all populate totalTokens consistently.
    values = (counts[0], counts[1], counts[2] - hour, hour, counts[3], reasoning)
    return dict(zip(TOKEN_FIELDS, values)), counts[2] > 0 and "cacheWrite1h" not in usage


def _moment(record, message):
    moment = parse_timestamp(record.get("timestamp"))
    if moment is not None:
        return moment
    stamp = message.get("timestamp")
    if type(stamp) in (int, float):
        try:
            return datetime.fromtimestamp(stamp / 1000, timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _identity(value):
    return value if isinstance(value, str) and value else "unknown"


def _origin(file, headers):
    """Follow parentSession metadata without opening any unselected files."""
    seen = set()
    while file in headers and file not in seen:
        seen.add(file)
        session_id, parent = headers[file]
        if parent is None:
            return ("session", session_id) if session_id else ("file", file)
        file = parent
    return ("file", file)


def _read_record(record, file, position, headers, candidates):
    if not isinstance(record, dict):
        raise ValueError("invalid record")
    kind = record.get("type")
    if kind == "session":
        session_id = record.get("id")
        parent = record.get("parentSession")
        if isinstance(parent, str) and parent:
            path = Path(parent).expanduser()
            parent = str((Path(file).parent / path).resolve())
        else:
            parent = None
        headers[file] = (session_id if isinstance(session_id, str) and session_id else None, parent)
        return
    if kind == "message":
        message = record["message"]
        if message.get("role") not in ("assistant", "toolResult"):
            return
    elif kind in ("usage", "compaction", "branch_summary"):
        message = record
    else:
        return
    if message.get("usage") is None:
        return
    usage, assumed = normalize(message["usage"])
    if not any(usage.values()):
        return
    provider, model = _identity(message.get("provider")), _identity(message.get("model"))
    model_key = f"{provider}/{model}"
    moment = _moment(record, message)
    response_id = message.get("responseId")
    entry_id = record.get("id")
    # Entry IDs are short and only session-local. Timestamp additionally protects
    # against collisions within a lineage; response IDs survive rewritten IDs.
    if isinstance(response_id, str) and response_id:
        key = ("response", provider, model, response_id)
    elif isinstance(entry_id, str) and entry_id:
        key = ("entry", entry_id, record.get("timestamp") if isinstance(record.get("timestamp"), str)
               else str(message.get("timestamp")), model_key, kind)
    else:
        key = ("position", file, *position)
    candidates.append((file, key, model_key, usage, assumed, moment,
                       provider == "unknown" or model == "unknown"))


def analyze(paths, period, group="model"):
    report = new_report("pi", paths, period, PI_PRICING, duplicate_snapshots=0,
                        updated_snapshots=0, assumed_5m_cache_events=0, unattributed_usage_events=0)
    headers, candidates = {}, []
    for file in logs.find_files(paths):
        report["files_scanned"] += 1
        try:
            for index, value in logs.read_values(file):
                for ordinal, record in enumerate(logs.records(value)):
                    try:
                        _read_record(record, str(file), (index, ordinal), headers, candidates)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        report["invalid_records"] += 1
        except (OSError, UnicodeError, ValueError, RecursionError) as error:
            report["files_failed"] += 1
            print(f"ai-costs: warning: pi: skipped {file} ({type(error).__name__})", file=sys.stderr)

    snapshots = {}
    for file, key, model, usage, assumed, moment, unattributed in candidates:
        if key[0] == "entry":
            key = (*key, _origin(file, headers))
        previous = snapshots.get(key)
        rank = (sum(usage[k] for k in BILLED_FIELDS), usage["reasoning_tokens"], moment is not None)
        if previous is not None:
            report["duplicate_snapshots"] += 1
            if rank <= previous[0]:
                continue
            report["updated_snapshots"] += 1
        snapshots[key] = (rank, model, usage, assumed, moment, unattributed)

    totals, unpriced = {}, set()
    for _, model, usage, assumed, moment, unattributed in snapshots.values():
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
        report["unattributed_usage_events"] += int(unattributed)
        row = totals.setdefault((model, day), zero())
        for field in TOKEN_FIELDS:
            row[field] += usage[field]
        prices = pi_prices(model)
        if prices and any(usage[k] and k not in prices for k in BILLED_FIELDS):
            unpriced.add(model)
    return finalize(report, totals, lambda model: None if model in unpriced else pi_prices(model), group)
