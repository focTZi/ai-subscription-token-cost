"""Shared report structure, cost calculation and terminal output."""
from .dates import bucket

TOKEN_FIELDS = (
    "uncached_input_tokens", "cached_input_tokens", "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens", "output_tokens", "reasoning_tokens",
)
# Reasoning/thinking is part of output_tokens and therefore not billed twice.
BILLED_FIELDS = TOKEN_FIELDS[:-1]
PROVIDER_NAMES = dict(codex="Codex", claude="Claude")
GROUP_TITLES = dict(day="Day", week="Week", month="Month")


def zero():
    return dict.fromkeys(TOKEN_FIELDS, 0)


def _add(target, counts):
    for field in TOKEN_FIELDS:
        target[field] += counts[field]


def new_report(provider, paths, period, pricing, **counters):
    return dict(provider=provider, source_paths=[str(p) for p in paths], pricing=pricing,
                period=period.to_json(), files_scanned=0, files_failed=0, usage_events=0,
                invalid_records=0, skipped_undated_usage_events=0, filtered_usage_events=0,
                **counters)


def _sum_totals(parts):
    """Combine totals that already carry costs (from _price or earlier sums)."""
    totals = zero()
    for part in parts:
        _add(totals, part)
    known = sum(p["known_cost_usd"] for p in parts)
    complete = all(p["pricing_complete"] for p in parts)
    totals.update(total_tokens=sum(totals[k] for k in BILLED_FIELDS), known_cost_usd=known,
                  pricing_complete=complete, cost_usd=known if complete else None)
    return totals


def _price(models, price_for):
    """Per-model rows with costs, plus their totals."""
    rows = []
    for name in sorted(models):
        row = dict(model=name, **models[name])
        prices = price_for(name)
        row["total_tokens"] = sum(row[k] for k in BILLED_FIELDS)
        row["prices_usd_per_million_tokens"] = prices
        row["cost_usd"] = sum(row[k] * v for k, v in prices.items()) / 1_000_000 if prices else None
        rows.append(row)
    parts = [dict(r, known_cost_usd=r["cost_usd"] or 0, pricing_complete=r["cost_usd"] is not None) for r in rows]
    return rows, _sum_totals(parts)


def finalize(report, usage, price_for, group="model"):
    """Turn usage keyed by (model, local day) into priced model rows and optional groups."""
    by_model, by_group = {}, {}
    for (model, day), counts in usage.items():
        _add(by_model.setdefault(model, zero()), counts)
        if group != "model":
            _add(by_group.setdefault(bucket(day, group), {}).setdefault(model, zero()), counts)
    report["models"], report["totals"] = _price(by_model, price_for)
    report["group_by"] = group
    if group != "model":
        report["groups"] = []
        for key in sorted(by_group):  # "undated" sorts after all dates.
            rows, totals = _price(by_group[key], price_for)
            report["groups"].append(dict(key=key, models=rows, totals=totals))
    return report


def combine(reports, period, group="model"):
    combined = dict(schema_version=2, currency="USD", period=period.to_json(), group_by=group,
                    providers=reports, totals=_sum_totals([r["totals"] for r in reports.values()]))
    if group != "model":
        keys = sorted({g["key"] for r in reports.values() for g in r["groups"]})
        combined["groups"] = []
        for key in keys:
            providers = {name: g["totals"] for name, r in reports.items() for g in r["groups"] if g["key"] == key}
            combined["groups"].append(dict(key=key, providers=providers, totals=_sum_totals(list(providers.values()))))
    return combined


def diagnostics(report):
    """Human-readable warnings for stderr; the same numbers are in the JSON report."""
    notes = []
    if not report["files_scanned"]:
        where = ", ".join(report["source_paths"]) or "the default location"
        notes.append(f"no log files found in {where}")
    if report["files_failed"]:
        notes.append(f"{report['files_failed']} unreadable or invalid file(s) skipped")
    if report["invalid_records"]:
        notes.append(f"{report['invalid_records']} invalid line(s) or usage record(s) skipped")
    if report["skipped_undated_usage_events"]:
        notes.append(f"{report['skipped_undated_usage_events']} usage record(s) without a valid timestamp skipped")
    if report.get("counter_resets"):
        notes.append(f"{report['counter_resets']} reset(s) of cumulative token counters detected")
    if report.get("assumed_5m_cache_events"):
        notes.append(f"{report['assumed_5m_cache_events']} cache write(s) without TTL priced as 5-minute cache")
    unpriced = [row["model"] for row in report["models"] if row["cost_usd"] is None]
    if unpriced:
        notes.append(f"no price for {', '.join(unpriced)}; total cost is incomplete")
    return notes


def human_tokens(count):
    for divisor, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= divisor:
            return f"{count / divisor:.2f}{unit}"
    return str(count)


def _money(value, complete=True):
    if value is None:
        return "n/a"
    text = "<0.01" if 0 < value < 0.005 else f"{value:,.2f}"
    return text if complete else text + " *"


def _cost(totals):
    return _money(totals["known_cost_usd"], totals["pricing_complete"])


def _layout(header, rows, left_columns):
    """Align columns; None in rows draws a separator line."""
    widths = [max(len(header[i]), *(len(r[i]) for r in rows if r)) for i in range(len(header))]

    def line(cells):
        return "  ".join(cell.ljust(w) if i < left_columns else cell.rjust(w)
                         for i, (cell, w) in enumerate(zip(cells, widths))).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return [line(header), rule] + [rule if row is None else line(row) for row in rows]


def _model_rows(combined, unpriced):
    def counts(row):
        write = row["cache_creation_5m_input_tokens"] + row["cache_creation_1h_input_tokens"]
        return [human_tokens(row["uncached_input_tokens"]), human_tokens(row["cached_input_tokens"]),
                human_tokens(write), human_tokens(row["output_tokens"]), human_tokens(row["total_tokens"])]

    rows = []
    for provider, report in combined["providers"].items():
        name = PROVIDER_NAMES.get(provider, provider)
        if not report["models"]:
            rows.append([name, "(no usage)", "", "", "", "", "", ""])
        for index, row in enumerate(report["models"]):
            rows.append([name if index == 0 else "", row["model"], *counts(row), _money(row["cost_usd"])])
            if row["cost_usd"] is None:
                unpriced.append(f"{name} {row['model']} ({human_tokens(row['total_tokens'])} tokens)")
        if len(report["models"]) > 1:
            rows.append(["", "subtotal", *counts(report["totals"]), _cost(report["totals"])])
        rows.append(None)
    rows.append(["Total", "", *counts(combined["totals"]), _cost(combined["totals"])])
    header = ("Provider", "Model", "Input", "Cache read", "Cache write", "Output", "Total", "Cost (USD)")
    return _layout(header, rows, 2)


def _group_rows(combined, unpriced):
    names = list(combined["providers"])
    for provider, report in combined["providers"].items():
        unpriced += [f"{PROVIDER_NAMES.get(provider, provider)} {row['model']} ({human_tokens(row['total_tokens'])} tokens)"
                     for row in report["models"] if row["cost_usd"] is None]
    rows = []
    for group in combined["groups"]:
        costs = [_cost(group["providers"][n]) if n in group["providers"] else "" for n in names]
        rows.append([group["key"], *costs, human_tokens(group["totals"]["total_tokens"]), _cost(group["totals"])])
    if not rows:
        rows.append(["(no usage)", *[""] * (len(names) + 2)])
    totals = combined["totals"]
    provider_costs = [_cost(combined["providers"][n]["totals"]) for n in names]
    rows += [None, ["Total", *provider_costs, human_tokens(totals["total_tokens"]), _cost(totals)]]
    header = (GROUP_TITLES[combined["group_by"]], *(f"{PROVIDER_NAMES.get(n, n)} (USD)" for n in names),
              "Tokens", "Cost (USD)")
    return _layout(header, rows, 1)


def render_table(combined):
    period = combined["period"]
    span = ""
    if period["from_date"] or period["to_date"]:
        span = f": {period['from_date'] or 'beginning'} to {period['to_date'] or 'now'}"
    lines = [f"Estimated API cost, {period['label']}{span} ({period['timezone']})", ""]
    unpriced = []
    if combined["group_by"] == "model":
        lines += _model_rows(combined, unpriced)
    else:
        lines += _group_rows(combined, unpriced)
    lines.append("")
    if unpriced:
        lines.append("* Known prices only. No price for: " + "; ".join(unpriced) + ".")
    lines.append("Total = input + cache read + cache write + output; reasoning is part of output.")
    lines.append("Estimate at pay-as-you-go API list prices, not an actual bill.")
    return "\n".join(lines)
