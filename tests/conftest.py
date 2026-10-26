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


@pytest.fixture(autouse=True)
def _credenciales_aisladas(tmp_path_factory, monkeypatch):
    """Ningún test lee /secretos de la máquina: el directorio de credenciales apunta a uno que no existe (la key sale
    de config.json, como antes de la migración). Los tests de credenciales lo cambian con monkeypatch."""
    from futuresboard import credenciales

    base = tmp_path_factory.mktemp("sin-secretos")
    monkeypatch.setenv(credenciales.ENV_DIR, str(base / "no-existe"))
    monkeypatch.setenv(credenciales.ENV_RESPALDO_DIR, str(base / "no-existe-respaldo"))
    monkeypatch.delenv(credenciales.ENV_USUARIO, raising=False)
