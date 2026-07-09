"""Exchange fetcher registry. Add new exchanges here."""
from .bybit import fetch as fetch_bybit
from .binance import fetch as fetch_binance

FETCHERS = [fetch_bybit, fetch_binance]
# To add OKX: from .okx import fetch as fetch_okx; append to FETCHERS
