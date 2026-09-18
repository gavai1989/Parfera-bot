from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

from fish import FISH_SPECIES

if TYPE_CHECKING:
    from openai import OpenAI

MODEL = "gpt-image-1.5"
QUALITY = "low"
INPUT_FIDELITY = "high"


def get_output_size(image_path: str | Path) -> str:
    """Choose an output size that matches the source photo orientation."""
    from PIL import Image

    with Image.open(image_path) as image:
        width, height = image.size

    if width == height:
        return "1024x1024"
    if height > width:
        return "1024x1536"
    return "1536x1024"


def build_prompt(fish: str) -> str:
    return f"""Edit the supplied fishing photograph. DO NOT recreate the photograph.
Treat the supplied image as the original finished photograph and make the smallest
possible local edit: add one realistic caught {fish} and leave everything else
unchanged.

HIGHEST PRIORITY — PRESERVE THE ORIGINAL PHOTO:
- Preserve the exact original people, faces, identity, body proportions, poses,
  hands, arms, legs, feet, clothing and clothing details.
- Preserve the exact original background and environment: location, landscape,
  water, shore, trees, buildings, furniture, objects, vehicles, boats and all
  other visible surroundings.
- Preserve the original lighting, shadows, colors, reflections, depth of field,
  perspective, camera angle, image grain and photographic character.
- Preserve the original framing and aspect ratio. Do not crop, zoom in, reframe,
  rotate, extend, redesign or replace the scene.
- Never cut off any visible part of a person. If a person is shown full-body,
  keep the entire person visible from head to feet.
- If preserving the original photo conflicts with adding the fish, prioritize
  preserving the original photo. It is better for the fish edit to be imperfect
  than to change the original scene.

ADD ONLY THE FISH:
- Add one believable, normally sized caught {fish}.
- Choose the person who can most naturally and plausibly hold the fish. If there
  are multiple people, do not rearrange or alter them.
- Place the fish naturally in the person's hands or in the most realistic
  position for a freshly caught fish.
- Match the original perspective, scale, lighting direction, shadows, reflections,
  occlusion, texture, moisture and image grain.
- The fish must look physically present in the original scene, as if it was
  actually caught and photographed there.
- Do not add or remove anything else.

ABSOLUTE NEGATIVES:
Do not change the background. Do not change the environment. Do not change the
location. Do not regenerate the people. Do not alter faces. Do not change clothes.
Do not crop the body. Do not zoom. Do not change the camera angle. Do not add
extra people, animals, equipment, scenery, text or captions.

Target fish: {fish}.
Available MVP species: {', '.join(FISH_SPECIES)}.
"""


def edit_fishing_photo(client: OpenAI, image_path: str | Path, fish: str) -> bytes:
    """Edit an input photo and return the generated image bytes."""
    if fish not in FISH_SPECIES:
        raise ValueError(f"Unsupported fish: {fish}")

    from openai import OpenAI

    if not isinstance(client, OpenAI):
        raise TypeError("client must be an OpenAI client")

    size = get_output_size(image_path)

    with open(image_path, "rb") as image_file:
        result = client.images.edit(
            model=MODEL,
            image=image_file,
            prompt=build_prompt(fish),
            quality=QUALITY,
            size=size,
            input_fidelity=INPUT_FIDELITY,
        )

    if not result.data or not getattr(result.data[0], "b64_json", None):
        raise RuntimeError("OpenAI did not return an image")

    return base64.b64decode(result.data[0].b64_json)
