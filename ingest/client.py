import logging
from typing import Any

import httpx

from config import settings

log = logging.getLogger(__name__)

PHASES = ("calendar", "symbols", "bars")


class AlpacaClient:
    """Authenticated transport for the Alpaca REST endpoints."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http if http is not None else httpx.Client(timeout=30.0)
        self._headers = {
            "APCA-API-KEY-ID": settings.ALPACA_KEY_ID,
            "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
        }
        self.request_counts = dict.fromkeys(PHASES, 0)
        self._logged: set[tuple[str, str]] = set()

    def __enter__(self) -> "AlpacaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def get_json(self, base_url: str, path: str, params: dict, phase: str) -> Any:
        # counted per phase because gate 1 extrapolates from the bars phase alone, while the calendar and symbols phases run once per invocation whatever the narrowing flags say.
        self.request_counts[phase] += 1
        request = self._http.build_request(
            "GET", base_url + path, params=params, headers=self._headers
        )
        if (base_url, path) not in self._logged:
            log.info("GET %s", request.url)
            self._logged.add((base_url, path))
        response = self._http.send(request)
        response.raise_for_status()
        return response.json()
