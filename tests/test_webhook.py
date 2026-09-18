from webhook import build_webhook_url, is_valid_webhook_secret


def test_build_webhook_url_normalizes_trailing_slash():
    assert build_webhook_url("https://fishing-bot.onrender.com/", "abc123") == (
        "https://fishing-bot.onrender.com/webhook/abc123"
    )


def test_webhook_secret_must_match_exactly():
    assert is_valid_webhook_secret("secret-123", "secret-123") is True
    assert is_valid_webhook_secret("secret-123", "secret-124") is False
    assert is_valid_webhook_secret("", "secret-123") is False
