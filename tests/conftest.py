import pytest

from futuresboard import telegram_notify


class _NoNetwork:
    def __call__(self, *args, **kwargs):
        raise AssertionError("Los tests no deben llamar a la API real de Telegram")


@pytest.fixture(autouse=True)
def _telegram_aislado(monkeypatch):
    """Ningún test toca api.telegram.org, aunque el shell tenga TELEGRAM_* configurado."""
    for name in (telegram_notify.ENV_TOKEN, telegram_notify.ENV_CHAT_ID, telegram_notify.ENV_PREFIX):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(telegram_notify, "_post", _NoNetwork())
