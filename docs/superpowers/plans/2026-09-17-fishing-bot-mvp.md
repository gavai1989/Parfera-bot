# Fishing Bot MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a minimal Telegram bot that accepts a fishing photo and returns a realistic edited photo with an automatically selected fish.

**Architecture:** Python Telegram bot using aiogram long polling. Photos are downloaded to a temporary file, sent to OpenAI GPT-Image-1.5 through the image-edit endpoint with low quality at 1024x1024, and the returned base64 image is sent back to Telegram. No database, payments, or persistent user data in the MVP.

**Tech Stack:** Python 3.11+, aiogram 3.x, OpenAI Python SDK, python-dotenv, pytest.

**Spec:** MVP design approved in chat on 2026-09-17.

## Global Constraints

- Telegram transport uses long polling.
- Model is `gpt-image-1.5`.
- Image quality is `low`.
- Output size is `1024x1024`.
- User does not select the fish in the MVP.
- MVP contains ten fish species.
- The edit must preserve people, faces, clothing, poses, background, and composition as much as possible.
- API keys are environment variables and never committed.

---

### Task 1: Project foundation

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `README.md`

- [x] Create dependency and environment files.
- [x] Document local startup and secret handling.
- [x] Verify secrets are excluded from git.

### Task 2: Fish selection

**Files:**
- Create: `fish.py`
- Test: `tests/test_fish.py`

- [x] Write tests for exactly ten unique MVP species.
- [x] Write a deterministic seeded selection path for testing.
- [x] Implement random production selection.
- [x] Run `pytest -q` and verify the fish tests pass.

### Task 3: OpenAI image editing

**Files:**
- Create: `image_editor.py`
- Test: `tests/test_image_editor.py`

- [x] Test the fixed model, quality, and output size.
- [x] Test that the prompt requires identity preservation and minimal realistic editing.
- [x] Implement GPT-Image-1.5 image editing using `client.images.edit`.
- [x] Decode the returned base64 image and fail clearly when no image is returned.
- [x] Run `pytest -q` and `python -m py_compile fish.py image_editor.py bot.py`.

### Task 4: Telegram long-polling bot

**Files:**
- Create: `bot.py`

- [x] Add `/start` handler with the approved fishing message.
- [x] Add photo handler that downloads the largest Telegram photo variant.
- [x] Select a fish automatically and call the image editor off the event loop.
- [x] Return the generated image and a short completion message.
- [x] Return a friendly error message while logging the exception server-side.
- [x] Keep temporary files isolated and delete them after each request.

### Task 5: Verification and handoff

**Files:**
- Modify: `README.md`

- [x] Run the unit tests.
- [x] Run Python syntax compilation.
- [ ] Configure real Telegram/OpenAI environment variables locally.
- [ ] Start the bot and send one real fishing photo.
- [ ] Review image quality and adjust the edit prompt if necessary.
