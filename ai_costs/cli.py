"""Command line interface: asks for provider and period when run in a terminal."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__, claude, codex, dates, pi
from .logs import LOG_SUFFIXES
from .report import PROVIDER_NAMES, combine, diagnostics, render_table

ANALYZERS = dict(codex=codex.analyze, claude=claude.analyze, pi=pi.analyze)
PERIOD_CHOICES = (
    ("last 30 days", lambda today: dates.last_days(30, today)),
    ("last 7 days", lambda today: dates.last_days(7, today)),
    ("this month", dates.this_month),
    ("last month", dates.last_month),
    ("all time", lambda today: dates.Period()),
    ("custom range", None),
)


def default_paths(provider):
    """Default log locations, honoring each tool's directory overrides."""
    if provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        candidates = (root / "sessions", root / "archived_sessions")
    elif provider == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        candidates = (root / "projects",)
    else:
        root = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent").expanduser()
        candidates = (Path(os.environ.get("PI_CODING_AGENT_SESSION_DIR") or root / "sessions").expanduser(),)
    return [p for p in candidates if p.exists()]


def installed_providers():
    found = [p for p in ANALYZERS if default_paths(p)]
    return found or list(ANALYZERS)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ai-costs",
        description="Estimate what your local Codex, Claude Code and Pi usage would cost at API list prices. "
                    "Reads local log files only; nothing is sent anywhere.",
        epilog="Run without --provider, --period/--from/--to and --by, ai-costs asks for them in a terminal. "
               "Days are calendar days in the system time zone (set TZ to change it) and inclusive.")
    parser.add_argument("-p", "--provider", choices=("all", "codex", "claude", "pi"),
                        help="which logs to analyze (default: every provider with local logs)")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--period", metavar="PERIOD",
                      help="30d (default), 7d, any Nd, this-month, last-month, YYYY-MM or all")
    when.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="first day to include")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="last day to include")
    parser.add_argument("--by", choices=dates.GROUPS,
                        help="break costs down by model (default), day, week (ISO) or month")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("-y", "--no-input", action="store_true", help="never ask; use defaults for anything not given")
    parser.add_argument("--codex-path", action="append", type=Path, metavar="PATH",
                        help="Codex log directory or file (repeatable; default: ~/.codex/sessions and archived_sessions)")
    parser.add_argument("--claude-path", action="append", type=Path, metavar="PATH",
                        help="Claude Code log directory or file (repeatable; default: ~/.claude/projects)")
    parser.add_argument("--pi-path", action="append", type=Path, metavar="PATH",
                        help="Pi log directory or file (repeatable; default: ~/.pi/agent/sessions)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _ask(prompt, inp, out):
    out.write(prompt)
    out.flush()
    answer = inp.readline()
    if not answer:  # EOF, e.g. Ctrl-D
        raise KeyboardInterrupt
    return answer.strip()


def choose(question, options, default, inp=sys.stdin, out=sys.stderr):
    """Numbered menu on stderr, so prompts never end up in redirected output."""
    out.write(f"{question}\n")
    for number, option in enumerate(options, 1):
        marker = "  (default)" if number - 1 == default else ""
        out.write(f"  {number}) {option}{marker}\n")
    while True:
        answer = _ask(f"Choose 1-{len(options)} [{default + 1}]: ", inp, out)
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        out.write(f"Please enter a number between 1 and {len(options)}.\n")


def ask_date(prompt, default, inp=sys.stdin, out=sys.stderr):
    while True:
        suffix = f" [{default}]" if default else ""
        answer = _ask(f"{prompt} (YYYY-MM-DD){suffix}: ", inp, out)
        if not answer and default:
            return default
        try:
            return dates.parse_date(answer)
        except ValueError as error:
            out.write(f"{error}\n")


def ask_provider(default, inp=sys.stdin, out=sys.stderr):
    options = ["All locally detected tools", "Codex only", "Claude only", "Pi only"]
    values = ["all", "codex", "claude", "pi"]
    return values[choose("Which usage do you want to analyze?", options, values.index(default), inp, out)]


def ask_period(today, inp=sys.stdin, out=sys.stderr):
    labels = [label for label, _ in PERIOD_CHOICES]
    index = choose("Which period?", labels, 0, inp, out)
    make = PERIOD_CHOICES[index][1]
    if make:
        return make(today)
    while True:
        start = ask_date("From", None, inp, out)
        end = ask_date("To", today, inp, out)
        try:
            return dates.Period(start, end)
        except ValueError as error:
            out.write(f"{error}\n")


def ask_group(inp=sys.stdin, out=sys.stderr):
    options = ["by model", "by day", "by week", "by month"]
    return dates.GROUPS[choose("How should costs be broken down?", options, 0, inp, out)]


def period_from_args(args, today):
    if args.period:
        if args.to_date:
            raise ValueError("--to can only be combined with --from")
        return dates.parse_period(args.period, today)
    if args.from_date or args.to_date:
        start = dates.parse_date(args.from_date) if args.from_date else None
        end = dates.parse_date(args.to_date) if args.to_date else None
        return dates.Period(start, end)
    return None


def resolve_paths(provider, given, parser):
    if not given:
        return default_paths(provider)
    paths = []
    for path in given:
        path = path.expanduser().absolute()
        if not path.exists():
            parser.error(f"{provider} path does not exist: {path}")
        if not path.is_dir() and path.suffix not in LOG_SUFFIXES:
            parser.error(f"{provider} path must be a directory or a .json/.jsonl file: {path}")
        paths.append(path)
    return paths


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    today = dates.local_today()
    try:
        period = period_from_args(args, today)
    except ValueError as error:
        parser.error(str(error))
    detected = installed_providers()
    provider = args.provider or ("all" if len(detected) > 1 else detected[0])
    group = args.by or "model"
    # Ask only when nothing was chosen on the command line; any option means "use defaults for the rest".
    chosen = args.provider or period or args.by or args.codex_path or args.claude_path or args.pi_path
    if not chosen and not args.no_input and sys.stdin.isatty() and sys.stderr.isatty():
        try:
            provider = ask_provider(provider)
            period = ask_period(today)
            group = ask_group()
            sys.stderr.write("\n")
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            return 130
    period = period or dates.last_days(30, today)

    if provider == "all":
        selected = list(ANALYZERS) if args.provider == "all" else detected
    else:
        selected = [provider]
    if not args.provider:
        selected = [name for name in ANALYZERS if name in selected or getattr(args, f"{name}_path")]
    reports = {}
    for name in selected:
        paths = resolve_paths(name, getattr(args, f"{name}_path"), parser)
        reports[name] = ANALYZERS[name](paths, period, group)
        for note in diagnostics(reports[name]):
            print(f"ai-costs: {PROVIDER_NAMES[name]}: {note}", file=sys.stderr)
    combined = combine(reports, period, group)
    if args.json:
        print(json.dumps(combined, indent=2, allow_nan=False))
    else:
        print(render_table(combined))
    return 0
