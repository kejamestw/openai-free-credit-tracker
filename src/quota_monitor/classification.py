INCENTIVIZED_TIERS = frozenset(
    {
        "incentivized-tier",
        "data-sharing",
        "data-sharing-incentive",
    }
)


def is_incentivized(service_tier: str | None) -> bool:
    value = (service_tier or "").strip().lower().replace("_", "-")
    return value in INCENTIVIZED_TIERS


def estimate_cost(input_tokens: int, cached_tokens: int, output_tokens: int, pricing: dict) -> float:
    safe_input = max(0, input_tokens)
    safe_cached = min(safe_input, max(0, cached_tokens))
    safe_output = max(0, output_tokens)
    uncached = safe_input - safe_cached
    return (
        uncached * pricing["input"]
        + safe_cached * pricing["cached_input"]
        + safe_output * pricing["output"]
    ) / 1_000_000


def cost_level(pricing: dict, low_below: float = 0.003, high_from: float = 0.012) -> str:
    sample_cost = (1000 * pricing["input"] + 1000 * pricing["output"]) / 1_000_000
    if sample_cost >= high_from:
        return "high"
    if sample_cost >= low_below:
        return "medium"
    return "low"
