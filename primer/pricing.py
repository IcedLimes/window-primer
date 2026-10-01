"""API-equivalent cost of a response, used as the "intensity" of usage.

Plan limits drain roughly in proportion to compute, so list price is a good
relative weight between models and token types. The absolute scale is
calibrated away by the planner, so these only need to be right relative to
each other. Tiers mirror Claude Code's baked model catalog (USD per MTok).
"""

# input, output, cache_write_5m, cache_write_1h, cache_read
TIERS = {
    "tier_2_10": (2, 10, 2.5, 4, 0.2),
    "tier_3_15": (3, 15, 3.75, 6, 0.3),
    "tier_4_20_cr_0_20": (4, 20, 5, 8, 0.2),
    "tier_5_25": (5, 25, 6.25, 10, 0.5),
    "tier_10_50": (10, 50, 12.5, 20, 1),
    "tier_15_75": (15, 75, 18.75, 30, 1.5),
    "haiku_35": (0.8, 4, 1, 1.6, 0.08),
    "haiku_45": (1, 5, 1.25, 2, 0.1),
}

MODEL_TIERS = {
    "claude-opus-5-5": "tier_4_20_cr_0_20",
    "claude-opus-5": "tier_5_25",
    "claude-opus-4-8": "tier_5_25",
    "claude-opus-4-5": "tier_5_25",
    "claude-opus-4-1": "tier_15_75",
    "claude-opus-4": "tier_15_75",
    "claude-fable-5-1": "tier_10_50",
    "claude-fable-5": "tier_10_50",
    "claude-sonnet-5": "tier_2_10",
    "claude-sonnet-4-5": "tier_3_15",
    "claude-sonnet-4": "tier_3_15",
    "claude-haiku-4-5": "haiku_45",
    "claude-3-5-haiku": "haiku_35",
}

FAMILY_TIERS = {"fable": "tier_10_50", "opus": "tier_5_25", "sonnet": "tier_3_15", "haiku": "haiku_45"}


def tier_for(model):
    model = (model or "").lower()
    # Longest prefix wins so "claude-opus-5-5" isn't matched as "claude-opus-5"; also strips date suffixes.
    for key in sorted(MODEL_TIERS, key=len, reverse=True):
        if model == key or model.startswith(key + "-") or model.startswith(key + "["):
            return TIERS[MODEL_TIERS[key]]
    for family, tier in FAMILY_TIERS.items():
        if family in model:
            return TIERS[tier]
    return TIERS["tier_5_25"]


def cost_usd(model, usage):
    inp, out, cw5, cw1h, cr = tier_for(model)
    cache_write = usage.get("cache_creation_input_tokens") or 0
    detail = usage.get("cache_creation") or {}
    write_1h = min(detail.get("ephemeral_1h_input_tokens") or 0, cache_write)
    total = ((usage.get("input_tokens") or 0) * inp
             + (usage.get("output_tokens") or 0) * out
             + (cache_write - write_1h) * cw5
             + write_1h * cw1h
             + (usage.get("cache_read_input_tokens") or 0) * cr)
    return total / 1e6
