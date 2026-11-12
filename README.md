# ai-costs

Estimate what your Codex and Claude Code usage would cost at API prices.

```bash
git clone https://github.com/focTZi/ai-subscription-token-cost.git
cd ai-subscription-token-cost
./ai-costs
```

You need Python 3.9 or newer. There are no other dependencies. Press Enter at each prompt to analyze both providers over the last 30 days, grouped by model.

`ai-costs` reads the local session logs from the [Codex CLI](https://github.com/openai/codex) and [Claude Code](https://code.claude.com), adds up the tokens per model and applies public pay-as-you-go API rates. Your logs stay on your machine and are never modified.

## Example

The numbers in this example are made up.

```
$ ./ai-costs
Which usage do you want to analyze?
  1) Codex and Claude  (default)
  2) Codex only
  3) Claude only
Choose 1-3 [1]:
Which period?
  1) last 30 days  (default)
  2) last 7 days
  3) this month
  4) last month
  5) all time
  6) custom range
Choose 1-6 [1]:
How should costs be broken down?
  1) by model  (default)
  2) by day
  3) by week
  4) by month
Choose 1-4 [1]:

Estimated API cost, last 30 days: 2026-10-14 to 2026-11-12 (CET)

Provider  Model               Input  Cache read  Cache write   Output    Total  Cost (USD)
--------  -----------------  ------  ----------  -----------  -------  -------  ----------
Codex     gpt-6-sol           2.31M      41.20M            0  310.50K   43.82M       15.96
--------  -----------------  ------  ----------  -----------  -------  -------  ----------
Claude    claude-opus-5-5    12.40K      88.10M        3.02M  402.10K   91.53M       40.81
          claude-sonnet-5-5   5.10K       9.40M      610.00K   95.30K   10.11M        3.43
          subtotal           17.50K      97.50M        3.63M  497.40K  101.64M       44.24
--------  -----------------  ------  ----------  -----------  -------  -------  ----------
Total                         2.33M     138.70M        3.63M  807.90K  145.47M       60.20

Total = input + cache read + cache write + output; reasoning is part of output.
Estimate at pay-as-you-go API list prices, not an actual bill.
```

## Usage

To install `ai-costs` as a command, run `pipx install .` (or `pip install .`) from the repository directory.

Without options, `ai-costs` asks what to analyze. Press Enter to accept a default. Pass `-p`, a period or `--by` to skip the prompts and use defaults for the remaining options. It also skips prompts with `-y` or when input is not a terminal, such as in cron, CI or pipes.

```bash
ai-costs -y                          # both providers, last 30 days
ai-costs -p claude --period 7d       # Claude Code, last 7 days
ai-costs --period last-month
ai-costs --period 2026-10            # one calendar month
ai-costs --from 2026-10-01 --to 2026-10-15
ai-costs --by day                    # costs per day, last 30 days
ai-costs --by month --period all
ai-costs --period all --json > costs.json
```

| Option | Meaning |
| --- | --- |
| `-p`, `--provider` | `both`, `codex` or `claude`. Default: every provider that has logs on this machine. |
| `--period` | `30d` (default), `7d`, any `Nd`, `this-month`, `last-month`, `YYYY-MM` or `all` |
| `--from`, `--to` | Custom range. Both days are included, and either one may be left out. |
| `--by` | `model` (default), `day`, `week` (ISO weeks) or `month` |
| `--json` | Full report, including per-model prices, groups and diagnostic counters |
| `-y`, `--no-input` | Never ask |
| `--codex-path`, `--claude-path` | Other log directories or single `.json`/`.jsonl` files (repeatable) |

Date ranges use calendar days in your system time zone. Set `TZ` to use another one (`TZ=UTC ai-costs`). Warnings go to stderr, so `--json` output stays valid.

### Log locations

| Provider | Default location |
| --- | --- |
| Codex | `~/.codex/sessions` and `~/.codex/archived_sessions` (or `$CODEX_HOME`) |
| Claude Code | `~/.claude/projects`, including subagent transcripts (or `$CLAUDE_CONFIG_DIR`) |

Claude Code deletes local transcripts after 30 days by default. Increase `cleanupPeriodDays` if you want to keep more history.

## How it counts

- Total tokens = input + cache read + cache write + output. Reasoning or thinking tokens are already included in output.
- Codex logs cumulative counters. `ai-costs` calculates usage from the difference between consecutive counter values within each session file, accounting for repeated snapshots and counter resets.
- Claude Code logs one record per content block and copies history into subagent and resumed sessions. `ai-costs` deduplicates messages by ID across all files and keeps the most complete snapshot of each message.
- The calculation uses only usage fields. It ignores prompts, answers and tool output.
- Models without a known price still show their tokens. Their cost shows as `n/a`, and totals are marked with `*` (`cost_usd: null` in JSON).

## Prices

API rates are stored in [`ai_costs/pricing.py`](ai_costs/pricing.py) and were last checked on 2026-11-12:

- OpenAI: <https://developers.openai.com/api/docs/pricing> (standard tier, short context)
- Anthropic: <https://platform.claude.com/docs/en/about-claude/pricing> (standard rates, 5-minute and 1-hour cache writes)

Prices change. Pull requests that update them are welcome.

## Limitations

The estimate has a few limits:

- Long-context surcharges, fast mode, batch discounts, regional pricing, server-tool fees and taxes are not included.
- Historical prices are not tracked; all usage is priced at today's rates.
- Models with prompt-size-dependent pricing (such as Claude Haiku 5.5) and models without published API prices (such as `codex-auto-review`) show tokens but no cost.
- The logs do not reliably say whether a request was billed to a subscription or an API key, so all usage is counted.
- Copies of the same Codex session in several files are not deduplicated, and truncated logs can over- or under-count.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests use synthetic logs only.

## License

See [LICENSE](LICENSE).
