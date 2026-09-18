from fish import FISH_SPECIES, choose_fish


def test_mvp_contains_exactly_ten_species():
    assert len(FISH_SPECIES) == 10
    assert len(set(FISH_SPECIES)) == 10


def test_seeded_choice_is_deterministic():
    assert choose_fish(seed=1) == choose_fish(seed=1)


def test_choice_is_from_mvp_species():
    assert choose_fish(seed=42) in FISH_SPECIES
