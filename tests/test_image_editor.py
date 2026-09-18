from PIL import Image

from fish import FISH_SPECIES
from image_editor import INPUT_FIDELITY, MODEL, QUALITY, build_prompt, get_output_size


def test_image_configuration_is_mvp():
    assert MODEL == "gpt-image-1.5"
    assert QUALITY == "low"
    assert INPUT_FIDELITY == "high"


def test_output_size_matches_photo_orientation(tmp_path):
    portrait = tmp_path / "portrait.jpg"
    landscape = tmp_path / "landscape.jpg"
    square = tmp_path / "square.jpg"

    Image.new("RGB", (800, 1200)).save(portrait)
    Image.new("RGB", (1200, 800)).save(landscape)
    Image.new("RGB", (1000, 1000)).save(square)

    assert get_output_size(portrait) == "1024x1536"
    assert get_output_size(landscape) == "1536x1024"
    assert get_output_size(square) == "1024x1024"


def test_prompt_preserves_original_scene():
    prompt = build_prompt(FISH_SPECIES[0])
    assert "DO NOT recreate the photograph" in prompt
    assert "Preserve the exact original background and environment" in prompt
    assert "Preserve the original framing and aspect ratio" in prompt
    assert "Never cut off any visible part of a person" in prompt
    assert "add one realistic caught" in prompt
    assert "Do not change the background" in prompt
