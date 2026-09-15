# PARFERA — production launcher
# This file is intentionally the Render entrypoint.
# It starts the latest approved PARFERA bot implementation from 13.09.2026.
#
# IMPORTANT:
# Keep bot_PARFERA_HOME_CLEAN.py in the same repository/folder.
# Render command remains: python bot.py

import asyncio
import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCE = BASE / "bot_PARFERA_HOME_CLEAN.py"

if not SOURCE.exists():
    raise RuntimeError(
        "Не найден bot_PARFERA_HOME_CLEAN.py. "
        "Положите этот файл рядом с bot.py в репозитории PARFERA."
    )

spec = importlib.util.spec_from_file_location("parfera_home_clean", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("Не удалось загрузить bot_PARFERA_HOME_CLEAN.py")

module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# The approved implementation contains its own main() and starts:
# - Telegram long polling
# - /health endpoint
# - PARFERA AI free-text handler
# - catalog search
# - cart/catalog/product handlers
if not hasattr(module, "main"):
    raise RuntimeError("В bot_PARFERA_HOME_CLEAN.py отсутствует main()")

if __name__ == "__main__":
    asyncio.run(module.main())
