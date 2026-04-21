"""Market data: instruments master, candle fetching + cache, holiday calendar."""

from data.holidays import DEFAULT_YAML_PATH, HolidayCalendar
from data.instruments import NSE_EQUITY_MASTER_URL, InstrumentMaster
from data.market_data import (
    CandleFetcher,
    FakeCandleFetcher,
    YFinanceFetcher,
    build_synthetic_candles,
    candles_from_csv,
    candles_to_csv,
    df_to_candles,
    load_candles_bulk,
    save_candles_bulk,
)
from data.universe import (
    IMPLEMENTED_PRESETS,
    KNOWN_PRESETS,
    PresetNotImplementedError,
    UniverseEntry,
    UniverseRegistry,
    UnknownSymbolError,
)

__all__ = [
    "CandleFetcher",
    "DEFAULT_YAML_PATH",
    "FakeCandleFetcher",
    "HolidayCalendar",
    "IMPLEMENTED_PRESETS",
    "InstrumentMaster",
    "KNOWN_PRESETS",
    "NSE_EQUITY_MASTER_URL",
    "PresetNotImplementedError",
    "UniverseEntry",
    "UniverseRegistry",
    "UnknownSymbolError",
    "YFinanceFetcher",
    "build_synthetic_candles",
    "candles_from_csv",
    "candles_to_csv",
    "df_to_candles",
    "load_candles_bulk",
    "save_candles_bulk",
]
