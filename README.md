# PARFERA — Telegram shop

Render-ready version: full supplier catalog + search.

- 29,434 supplier positions in `catalog.json`
- Prices converted to RUB at supplier rate 90
- Search by product name or article
- 10 catalog/search results per page
- Telegram webhook is removed automatically before long polling
- HTTP `/health` endpoint listens on Render's `$PORT`

## Render

Build command:
`pip install -r requirements.txt`

Start command:
`python bot.py`

Environment variable:
`PARFERA_BOT_TOKEN`

Optional Render Health Check Path:
`/health`
