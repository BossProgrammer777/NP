"""Асинхронный клиент API Новой Почты.

Единственный эндпоинт API — POST https://api.novaposhta.ua/v2.0/json/.
Тело любого запроса одинаковое:

    {
        "apiKey": "...",
        "modelName": "Address",
        "calledMethod": "getWarehouses",
        "methodProperties": { ... }
    }

Ответ всегда содержит поля success/data/errors/warnings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api.novaposhta.ua/v2.0/json/"

# Таймаут на один запрос и параметры ретраев.
REQUEST_TIMEOUT = 15.0
MAX_RETRIES = 4  # плюс первая попытка => максимум 5 обращений
RETRY_BASE_DELAY = 1.0  # сетевые/5xx: 1с, 2с, ...
RATE_LIMIT_BASE_DELAY = 2.0  # «too many requests»: 2с, 4с, 8с, ...
MAX_CONCURRENCY = 5  # не долбим API больше 5 запросов одновременно

# Подстроки, по которым опознаём rate-limit НП (приходит как success=false).
RATE_LIMIT_MARKERS = ("many request", "too many", "перевищено", "часто")


def _is_rate_limited(messages: list[Any]) -> bool:
    text = " ".join(str(m) for m in messages).lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


class NovaPoshtaError(Exception):
    """Ошибка бизнес-логики API НП (success == false).

    Несёт человекочитаемый текст, который можно показать пользователю.
    """

    def __init__(self, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or []


class NovaPoshtaClient:
    """Клиент API Новой Почты.

    Использование::

        async with NovaPoshtaClient(api_key) as np:
            types = await np.get_warehouse_types()
    """

    def __init__(self, api_key: str, api_url: str = API_URL):
        self._api_key = api_key
        self._api_url = api_url
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NovaPoshtaClient":
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Позволяет использовать клиент и без контекст-менеджера.
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        return self._client

    async def call(
        self, model: str, method: str, props: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Один общий метод обращения к API.

        Возвращает список словарей из поля ``data``. При ``success: false``
        поднимает :class:`NovaPoshtaError` с текстом из ``errors``.
        """
        payload = {
            "apiKey": self._api_key,
            "modelName": model,
            "calledMethod": method,
            "methodProperties": props or {},
        }

        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            async with self._semaphore:
                started = time.monotonic()
                try:
                    resp = await self._http.post(self._api_url, json=payload)
                    elapsed = time.monotonic() - started
                    logger.info(
                        "NP %s/%s -> %s за %.2fс",
                        model,
                        method,
                        resp.status_code,
                        elapsed,
                    )

                    # 5xx считаем временной ошибкой и ретраим.
                    if resp.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"server error {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    resp.raise_for_status()
                    body = resp.json()
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    # Сетевые ошибки и 5xx — ретраим с экспоненциальной задержкой.
                    last_exc = exc
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2**attempt)
                        logger.warning(
                            "NP %s/%s попытка %d не удалась (%s), повтор через %.1fс",
                            model,
                            method,
                            attempt + 1,
                            exc,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise NovaPoshtaError(
                        "Сервис Новой Почты временно недоступен, попробуйте позже"
                    ) from exc

            # Разбор бизнес-ответа делаем вне семафора.
            if not body.get("success", False):
                errors = body.get("errors") or []
                warnings = body.get("warnings") or []
                text = "; ".join(str(e) for e in (errors or warnings)) or (
                    "Новая Почта вернула ошибку без описания"
                )
                # «Too many requests» НП отдаёт как success=false — это не
                # фатальная ошибка, а лимит частоты. Ретраим с бэкоффом.
                if _is_rate_limited(errors + warnings) and attempt < MAX_RETRIES:
                    delay = RATE_LIMIT_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "NP %s/%s rate limit, повтор через %.1fс (попытка %d)",
                        model,
                        method,
                        delay,
                        attempt + 1,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("NP %s/%s success=false: %s", model, method, text)
                raise NovaPoshtaError(text, errors=[str(e) for e in errors])

            return body.get("data") or []

        # Сюда попасть не должны, но на всякий случай.
        raise NovaPoshtaError(
            "Сервис Новой Почты временно недоступен, попробуйте позже"
        ) from last_exc

    # --- Конкретные методы API -------------------------------------------

    async def get_settlements(
        self, page: int = 1, limit: int = 150, warehouse_only: bool = True
    ) -> list[dict[str, Any]]:
        """Справочник населённых пунктов (постранично)."""
        props: dict[str, Any] = {"Page": str(page), "Limit": str(limit)}
        if warehouse_only:
            props["Warehouse"] = "1"
        return await self.call("Address", "getSettlements", props)

    async def search_settlements(
        self, city_name: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Быстрый онлайн-поиск города (без кэша)."""
        return await self.call(
            "Address",
            "searchSettlements",
            {"CityName": city_name, "Limit": str(limit)},
        )

    async def get_warehouse_types(self) -> list[dict[str, Any]]:
        """Типы отделений (в т.ч. грузовое)."""
        return await self.call("Address", "getWarehouseTypes", {})

    async def get_warehouses(
        self,
        city_ref: str | None = None,
        type_ref: str | None = None,
        page: int = 1,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Отделения (постранично, с фильтрами)."""
        props: dict[str, Any] = {"Page": str(page), "Limit": str(limit)}
        if city_ref:
            props["CityRef"] = city_ref
        if type_ref:
            props["TypeOfWarehouseRef"] = type_ref
        return await self.call("Address", "getWarehouses", props)

    async def get_document_price(
        self,
        city_sender: str,
        city_recipient: str,
        weight: float,
        cost: float,
        service_type: str,
        cargo_type: str = "Cargo",
        seats_amount: int = 1,
        options_seat: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Расчёт стоимости доставки."""
        props: dict[str, Any] = {
            "CitySender": city_sender,
            "CityRecipient": city_recipient,
            "Weight": str(weight),
            "ServiceType": service_type,
            "Cost": str(int(cost)),
            "CargoType": cargo_type,
            "SeatsAmount": str(seats_amount),
        }
        if options_seat:
            props["OptionsSeat"] = options_seat
        return await self.call("InternetDocument", "getDocumentPrice", props)

    async def get_document_delivery_date(
        self,
        city_sender: str,
        city_recipient: str,
        service_type: str,
        date_time: str,
    ) -> list[dict[str, Any]]:
        """Расчёт ориентировочного срока доставки.

        ``date_time`` — дата отправки в формате ``dd.mm.yyyy``.
        """
        return await self.call(
            "InternetDocument",
            "getDocumentDeliveryDate",
            {
                "DateTime": date_time,
                "ServiceType": service_type,
                "CitySender": city_sender,
                "CityRecipient": city_recipient,
            },
        )
