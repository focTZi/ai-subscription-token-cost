"""Shared report structure, cost calculation and terminal output."""

TOKEN_FIELDS = (
    "uncached_input_tokens", "cached_input_tokens", "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens", "output_tokens", "reasoning_tokens",
)
# Reasoning/thinking is part of output_tokens and therefore not billed twice.
BILLED_FIELDS = TOKEN_FIELDS[:-1]
PROVIDER_NAMES = dict(codex="Codex", claude="Claude")


def zero():
    return dict.fromkeys(TOKEN_FIELDS, 0)


def new_report(provider, paths, period, pricing, **counters):
    return dict(provider=provider, source_paths=[str(p) for p in paths], pricing=pricing,
                period=period.to_json(), files_scanned=0, files_failed=0, usage_events=0,
                invalid_records=0, skipped_undated_usage_events=0, filtered_usage_events=0,
                **counters)


def finalize(report, models, price_for):
    """Attach per-model rows with costs and the provider totals to the report."""
    rows, totals, known = [], zero(), 0.0
    for name in sorted(models):
        row = dict(model=name, **models[name])
        prices = price_for(name)
        row["total_tokens"] = sum(row[k] for k in BILLED_FIELDS)
        row["prices_usd_per_million_tokens"] = prices
        row["cost_usd"] = sum(row[k] * v for k, v in prices.items()) / 1_000_000 if prices else None
        known += row["cost_usd"] or 0
        for field in TOKEN_FIELDS:
            totals[field] += row[field]
        rows.append(row)
    complete = all(row["cost_usd"] is not None for row in rows)
    totals.update(total_tokens=sum(totals[k] for k in BILLED_FIELDS), known_cost_usd=known,
                  pricing_complete=complete, cost_usd=known if complete else None)
    report.update(models=rows, totals=totals)
    return report


def combine(reports, period):
    totals = zero()
    for report in reports.values():
        for field in TOKEN_FIELDS:
            totals[field] += report["totals"][field]
    known = sum(r["totals"]["known_cost_usd"] for r in reports.values())
    complete = all(r["totals"]["pricing_complete"] for r in reports.values())
    totals.update(total_tokens=sum(totals[k] for k in BILLED_FIELDS), known_cost_usd=known,
                  pricing_complete=complete, cost_usd=known if complete else None)
    return dict(schema_version=2, currency="USD", period=period.to_json(),
                providers=reports, totals=totals)


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


def render_table(combined):
    period = combined["period"]
    lines = []
    span = ""
    if period["from_date"] or period["to_date"]:
        span = f": {period['from_date'] or 'beginning'} to {period['to_date'] or 'now'} (UTC)"
    lines.append(f"Estimated API cost, {period['label']}{span}")
    lines.append("")
    header = ("Provider", "Model", "Input", "Cache read", "Cache write", "Output", "Total", "Cost (USD)")
    rows, unpriced = [], []

    def counts(row):
        write = row["cache_creation_5m_input_tokens"] + row["cache_creation_1h_input_tokens"]
        return [human_tokens(row["uncached_input_tokens"]), human_tokens(row["cached_input_tokens"]),
                human_tokens(write), human_tokens(row["output_tokens"]), human_tokens(row["total_tokens"])]

    for provider, report in combined["providers"].items():
        name = PROVIDER_NAMES.get(provider, provider)
        if not report["models"]:
            rows.append([name, "(no usage)", "", "", "", "", "", ""])
        for index, row in enumerate(report["models"]):
            rows.append([name if index == 0 else "", row["model"], *counts(row), _money(row["cost_usd"])])
            if row["cost_usd"] is None:
                unpriced.append(f"{name} {row['model']} ({human_tokens(row['total_tokens'])} tokens)")
        if len(report["models"]) > 1:
            totals = report["totals"]
            rows.append(["", "subtotal", *counts(totals), _money(totals["known_cost_usd"], totals["pricing_complete"])])
        rows.append(None)
    totals = combined["totals"]
    rows.append(["Total", "", *counts(totals), _money(totals["known_cost_usd"], totals["pricing_complete"])])

    widths = [max(len(header[i]), *(len(r[i]) for r in rows if r)) for i in range(len(header))]

    def line(cells):
        left = [cells[i].ljust(widths[i]) for i in range(2)]
        right = [cells[i].rjust(widths[i]) for i in range(2, len(cells))]
        return "  ".join(left + right).rstrip()

    rule = "  ".join("-" * w for w in widths)
    lines += [line(header), rule]
    for row in rows:
        lines.append(rule if row is None else line(row))
    lines.append("")
    if unpriced:
        lines.append("* Known prices only. No price for: " + "; ".join(unpriced) + ".")
    lines.append("Total = input + cache read + cache write + output; reasoning is part of output.")
    lines.append("Estimate at pay-as-you-go API list prices, not an actual bill.")
    return "\n".join(lines)
