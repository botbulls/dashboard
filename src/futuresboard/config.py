from __future__ import annotations

import copy
import enum
import json
import os
import pathlib
from typing import List
from typing import Optional

from pydantic import BaseModel
from pydantic import DirectoryPath
from pydantic import Field
from pydantic import IPvAnyInterface
from pydantic import root_validator
from pydantic import validator


class NavbarBG(enum.Enum):
    BG_DARK = "bg-dark"
    BG_PRIMARY = "bg-primary"
    BG_SECONDARY = "bg-secondary"
    BG_SUCCESS = "bg-success"
    BG_DANGER = "bg-danger"
    BG_WARNING = "bg-warning"
    BG_INFO = "bg-info"
    BG_LIGHT = "bg-light"


# Binance Futures "demo trading" (reemplazo de testnet.binancefuture.com, que ccxt
# marca como no soportado para futuros). Solo se usa si FUTURESBOARD_BINANCE_TESTNET
# esta activo; con la config por defecto no se toca.
BINANCE_FUTURES_TESTNET_URL = "https://demo-fapi.binance.com"
BINANCE_FUTURES_MAINNET_URL = "https://fapi.binance.com"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Mueven solo la DB / el intervalo del scraper, por servicio y sin editar config.json (que tiene las
# API keys). Ver docs/produccion.md, "Scraper en su propio contenedor y SQLite en WAL".
ENV_DATABASE = "FUTURESBOARD_DATABASE"
ENV_AUTO_SCRAPE_INTERVAL = "FUTURESBOARD_AUTO_SCRAPE_INTERVAL"


def state_dir(config) -> pathlib.Path:
    """Directorio de los archivos de estado del panel, rescate, C3 y jobs.

    Acepta un ``Config`` o el ``app.config`` de Flask. Si no trae ``STATE_DIR`` (configs armadas a
    mano) es, como siempre, el directorio de la DB.
    """
    get = config.get if hasattr(config, "get") else (lambda key: getattr(config, key, None))
    value = get("STATE_DIR")
    return pathlib.Path(value) if value else pathlib.Path(get("DATABASE")).parent


class Exchanges(enum.Enum):
    BINANCE = "binance"
    BYBIT = "bybit"


class Custom(BaseModel):
    NAVBAR_TITLE: Optional[str] = Field("Futuresboard", min_length=1, max_length=50)
    NAVBAR_BG: Optional[NavbarBG] = NavbarBG.BG_DARK
    PROJECTIONS: List[float] = Field([1.003, 1.005, 1.01, 1.012], min_items=1, max_items=10)

    @validator("PROJECTIONS", each_item=True)
    @classmethod
    def _validate_projections(cls, value):
        if not isinstance(value, float):
            try:
                value = float(value)
            except TypeError:
                raise ValueError(f"Cannot cast {value!r} to a float")
        if value < -3.0:
            raise ValueError("The lower allowed projection value is -3.0")
        if value > 3.0:
            raise ValueError("The upper allowed projection value is 3.0")
        return value


class Config(BaseModel):
    CONFIG_DIR: DirectoryPath = pathlib.Path.cwd()
    DATABASE: Optional[pathlib.Path]
    # Estado del panel/rescate/C3/jobs. Default: el directorio de DATABASE tal como sale de
    # config.json (o CONFIG_DIR), ANTES de FUTURESBOARD_DATABASE: mover la DB por env no mueve el estado.
    STATE_DIR: Optional[pathlib.Path] = None
    EXCHANGE: Optional[Exchanges] = Exchanges.BINANCE
    TEST_MODE: Optional[bool] = False
    BINANCE_TESTNET: bool = False
    API_BASE_URL: Optional[str]
    AUTO_SCRAPE_INTERVAL: int = 300
    DISABLE_AUTO_SCRAPE: bool = False
    HOST: Optional[IPvAnyInterface] = IPvAnyInterface.validate("0.0.0.0")  # type: ignore[assignment]
    PORT: Optional[int] = Field(5000, ge=1, le=65535)
    # Vacías = la key sale de /secretos/api-keys.json (futuresboard.credenciales). Mientras dure la migración siguen
    # en config.json como respaldo; después se sacan de ahí y la app tiene que seguir arrancando.
    API_KEY: str = ""
    API_SECRET: str = ""

    CUSTOM: Optional[Custom] = Custom()

    @validator("DATABASE", always=True)
    @classmethod
    def _validate_database(cls, value, values):
        if not value:
            value = values["CONFIG_DIR"] / "futures.db"
        return value.resolve()

    @validator("STATE_DIR", always=True)
    @classmethod
    def _validate_state_dir(cls, value, values):
        if value:
            return value.resolve()
        database = values.get("DATABASE")
        return database.parent if database else None

    @root_validator(skip_on_failure=True)
    @classmethod
    def _database_from_env(cls, values):
        # Corre después de STATE_DIR: solo se mueven la DB y lo que vive junto a ella
        # (-wal/-shm, scrape_state.json, latido del scraper).
        raw = os.environ.get(ENV_DATABASE, "").strip()
        if raw:
            values["DATABASE"] = pathlib.Path(raw).resolve()
        return values

    @validator("DISABLE_AUTO_SCRAPE", always=True)
    @classmethod
    def _validate_disable_auto_scrape(cls, value):
        # config.json (DISABLE_AUTO_SCRAPE: true), env FUTURESBOARD_DISABLE_AUTO_SCRAPE=1
        # o el flag --disable-auto-scraper del CLI (ver cli.py).
        return bool(value) or _env_flag("FUTURESBOARD_DISABLE_AUTO_SCRAPE")

    @validator("BINANCE_TESTNET", always=True)
    @classmethod
    def _validate_binance_testnet(cls, value, values):
        # Opt-in: config.json (BINANCE_TESTNET: true) o env FUTURESBOARD_BINANCE_TESTNET=1.
        # Solo aplica a Binance.
        enabled = bool(value) or _env_flag("FUTURESBOARD_BINANCE_TESTNET")
        return enabled and values.get("EXCHANGE") == Exchanges.BINANCE

    @validator("API_BASE_URL", always=True)
    @classmethod
    def _validate_api_base_url(cls, value, values):
        if values.get("BINANCE_TESTNET"):
            # Forzado aunque config.json tenga API_BASE_URL, para no mezclar
            # el modo de prueba con el endpoint real.
            return (
                os.environ.get("FUTURESBOARD_BINANCE_TESTNET_URL", "").strip().rstrip("/")
                or BINANCE_FUTURES_TESTNET_URL
            )
        if not value:
            if values["EXCHANGE"] == Exchanges.BINANCE:
                if values["TEST_MODE"]:
                    value = "https://testnet.binancefuture.com"
                else:
                    value = "https://fapi.binance.com"
            elif values["EXCHANGE"] == Exchanges.BYBIT:
                if values["TEST_MODE"]:
                    value = "https://api-testnet.bybit.com"
                else:
                    value = "https://api.bybit.com"
        return value

    @validator("AUTO_SCRAPE_INTERVAL", pre=True, always=True)
    @classmethod
    def _auto_scrape_interval_from_env(cls, value):
        # El env gana sobre config.json; el rango 60-3600 lo valida el validador de abajo.
        raw = os.environ.get(ENV_AUTO_SCRAPE_INTERVAL, "").strip()
        return raw if raw else value

    @validator("AUTO_SCRAPE_INTERVAL")
    @classmethod
    def _validate_projections(cls, value):
        if value < 60:
            raise ValueError("The lower allowed value is 60")
        if value > 3600:
            raise ValueError("The upper allowed value is 3600")
        return value

    @root_validator(pre=True)
    @classmethod
    def _capitalize_all_keys(cls, fields):
        def _capitalize_keys(c):
            if not isinstance(c, dict):
                return c
            for key, value in copy.deepcopy(c).items():
                if isinstance(value, dict):
                    _capitalize_keys(value)
                c[key.upper()] = value
                if not key.isupper():
                    c.pop(key)

        for key, value in copy.deepcopy(fields).items():
            _capitalize_keys(value)
            fields[key.upper()] = value
            if not key.isupper():
                fields.pop(key)

        return fields

    @classmethod
    def from_config_dir(cls, config_dir: pathlib.Path) -> Config:
        config_file = config_dir / "config.json"
        if config_file.exists():
            config_dict = json.loads(config_file.read_text())
        else:
            config_dict = {}
        config_dict["CONFIG_DIR"] = config_dir
        return cls.parse_obj(config_dict)
