"""Fixed API list prices in USD per million tokens. Update these when prices change."""
import re

# Standard tier, short context (up to 272K input tokens).
CODEX_PRICING = dict(
    source="https://developers.openai.com/api/docs/pricing",
    checked="2026-11-12",
    basis="OpenAI standard tier, short context (<= 272K input tokens)",
)
CODEX_PRICES = {  # model: (input, cached input, output)
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6.1-terra": (1.00, 0.05, 5.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.3-codex": (1.75, 0.175, 14.00),
}

# Cache writes cost 1.25x (5-minute TTL) or 2x (1-hour TTL) the input price.
CLAUDE_PRICING = dict(
    source="https://platform.claude.com/docs/en/about-claude/pricing",
    checked="2026-11-12",
    basis="Anthropic standard global token prices; cache writes 1.25x (5 min) / 2x (1 h) input",
)
# Claude Haiku 5.5 is missing on purpose: its price depends on each prompt's size.
CLAUDE_PRICES = {  # model: (input, cache read, output)
    "claude-fable-5-2": (8, 0.20, 40),
    "claude-mythos-5-2": (8, 0.20, 40),
    "claude-fable-5-1": (10, 0.25, 50),
    "claude-mythos-5-1": (10, 0.25, 50),
    "claude-fable-5": (10, 1, 50),
    "claude-mythos-5": (10, 1, 50),
    "claude-opus-5-5": (4, 0.20, 20),
    "claude-sonnet-5-5": (2, 0.10, 10),
    "claude-opus-5": (5, 0.50, 25),
    "claude-sonnet-5": (2, 0.20, 10),
    **{f"claude-opus-4-{v}": (5, 0.50, 25) for v in (5, 6, 7, 8)},
    "claude-opus-4-1": (15, 1.50, 75),
    "claude-opus-4": (15, 1.50, 75),
    "claude-sonnet-4-6": (3, 0.30, 15),
    "claude-sonnet-4-5": (3, 0.30, 15),
    "claude-sonnet-4": (3, 0.30, 15),
    "claude-haiku-4-5": (1, 0.10, 5),
}


def codex_prices(model):
    p = CODEX_PRICES.get(model.lower())
    if p is None:
        return None
    return dict(uncached_input_tokens=p[0], cached_input_tokens=p[1], output_tokens=p[2])


def claude_prices(model):
    # Dated IDs and Claude Code's "[1m]" context suffix use the base model's rate.
    base = re.sub(r"\[1m\]$", "", model.lower())
    base = re.sub(r"-\d{8}$", "", base)
    p = CLAUDE_PRICES.get(base)
    if p is None:
        return None
    return dict(uncached_input_tokens=p[0], cached_input_tokens=p[1],
                cache_creation_5m_input_tokens=p[0] * 1.25,
                cache_creation_1h_input_tokens=p[0] * 2, output_tokens=p[2])
