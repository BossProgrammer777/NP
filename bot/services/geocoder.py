"""Геокодер адресов.

Абстрактный интерфейс + реализация на Nominatim (OpenStreetMap).
Смена провайдера на Google делается одной строкой в конфиге
(GEOCODER_PROVIDER=google) без правок в хендлерах.
"""

from __future__ import annotations

import abc
import asyncio
import logging

import httpx

from bot.services.cache import Cache

logger = logging.getLogger(__name__)


class Geocoder(abc.ABC):
    """Интерфейс геокодера."""

    @abc.abstractmethod
    async def geocode(self, query: str) -> tuple[float, float] | None:
        """Возвращает (lat, lon) или None, если адрес не найден."""

    async def aclose(self) -> None:  # pragma: no cover - опционально
        pass


class NominatimGeocoder(Geocoder):
    """Геокодер поверх Nominatim.

    Условия использования Nominatim:
    - обязательный осмысленный User-Agent с контактом (иначе бан по IP);
    - не больше 1 запроса в секунду (рейт-лимит через Lock + sleep);
    - результаты кэшируем в SQLite по нормализованной строке адреса.
    """

    def __init__(
        self,
        cache: Cache,
        user_agent: str,
        base_url: str = "https://nominatim.openstreetmap.org/search",
        min_interval: float = 1.1,
    ):
        self._cache = cache
        self._base_url = base_url
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": user_agent},
        )

    @staticmethod
    def _norm_key(query: str) -> str:
        return " ".join(query.lower().split())

    async def geocode(self, query: str) -> tuple[float, float] | None:
        key = self._norm_key(query)

        cached = await self._cache.get_geocode(key)
        if cached is not None:
            logger.info("geocode cache hit: %s", key)
            return cached

        # Рейт-лимит: не чаще одного запроса в секунду.
        async with self._lock:
            wait = self._min_interval - (
                asyncio.get_event_loop().time() - self._last_request_at
            )
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client.get(
                    self._base_url,
                    params={
                        "q": query,
                        "format": "json",
                        "limit": 1,
                        "countrycodes": "ua",
                    },
                )
                self._last_request_at = asyncio.get_event_loop().time()
                resp.raise_for_status()
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("geocode error для %r: %s", query, exc)
                return None

        if not data:
            logger.info("geocode: адрес не найден: %s", query)
            return None

        try:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
        except (KeyError, ValueError, IndexError):
            return None

        await self._cache.save_geocode(key, lat, lon)
        return lat, lon

    async def aclose(self) -> None:
        await self._client.aclose()


def build_geocoder(cache: Cache, settings) -> Geocoder:
    """Фабрика геокодера по конфигу.

    Здесь же место для ветки под Google Geocoding, когда понадобится.
    """
    provider = (settings.geocoder_provider or "nominatim").lower()
    if provider == "nominatim":
        return NominatimGeocoder(
            cache,
            user_agent=settings.geocoder_user_agent,
            base_url=settings.nominatim_url,
        )
    raise ValueError(f"Неизвестный geocoder_provider: {provider!r}")
