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


class _SinRedBinance:
    def __call__(self, *args, **kwargs):
        raise AssertionError("Los tests no deben llamar a la API pública de Binance: usar tests/estrategia_fakes.Red")


@pytest.fixture(autouse=True)
def _binance_publico_aislado(monkeypatch):
    """Ningún test sale a Binance desde Estrategia o los chequeos de forager, y la caché arranca vacía."""
    from futuresboard import binance_publico

    binance_publico.limpiar_cache()
    monkeypatch.setattr(binance_publico, "_get", _SinRedBinance())
    yield
    binance_publico.limpiar_cache()
