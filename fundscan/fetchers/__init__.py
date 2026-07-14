"""Exchange fetcher registry. Add new exchanges here."""
from .bybit import fetch as fetch_bybit
from .binance import fetch as fetch_binance
from .okx import fetch as fetch_okx
from .hyperliquid import fetch as fetch_hyperliquid

FETCHERS = [fetch_bybit, fetch_binance, fetch_okx, fetch_hyperliquid]
