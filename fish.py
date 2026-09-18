from __future__ import annotations

FISH_SPECIES = (
    "щука",
    "судак",
    "окунь",
    "сом",
    "карп",
    "сазан",
    "лещ",
    "налим",
    "жерех",
    "язь",
)


def choose_fish(seed: int | None = None) -> str:
    """Choose a fish for the MVP without asking the user.

    A deterministic seed is supported so the behavior can be tested.
    Production calls intentionally use a random choice.
    """
    import random

    rng = random.Random(seed)
    return rng.choice(FISH_SPECIES)
