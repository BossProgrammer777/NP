"""Локальный кэш справочников Новой Почты в SQLite.

НП требует хранить копию справочников у себя и обновлять раз в сутки.
Отделений больше 50 тысяч — дёргать API на каждое сообщение нельзя.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import aiosqlite

from bot.services.geo import haversine_km
from bot.services.novaposhta import NovaPoshtaClient, NovaPoshtaError

logger = logging.getLogger(__name__)

# Ключевые слова, по которым опознаём грузовое отделение среди типов.
CARGO_TYPE_KEYWORDS = ("вантаж",)  # «Вантажне відділення»

# Статусы отделений, которые считаем рабочими.
WORKING_STATUS = "Working"

# Пауза между постраничными запросами — чтобы не упираться в rate limit НП.
PAGE_DELAY = 0.35

SCHEMA = """
CREATE TABLE IF NOT EXISTS settlements (
    ref TEXT PRIMARY KEY,
    description TEXT,
    area TEXT,
    region TEXT,
    settlement_type TEXT,
    latitude REAL,
    longitude REAL
);

CREATE TABLE IF NOT EXISTS warehouses (
    ref TEXT PRIMARY KEY,
    number TEXT,
    description TEXT,
    short_address TEXT,
    city_ref TEXT,
    type_ref TEXT,
    latitude REAL,
    longitude REAL,
    total_max_weight REAL,
    place_max_weight REAL,
    schedule TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    latitude REAL,
    longitude REAL,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_warehouses_city_type
    ON warehouses (city_ref, type_ref);
CREATE INDEX IF NOT EXISTS idx_settlements_description
    ON settlements (description);
"""

# --- Нормализация украинских названий -----------------------------------

_APOSTROPHES = "'`ʼ’‘"
_TRANSLATE = str.maketrans(
    {
        "і": "и",
        "ї": "и",
        "й": "и",
        "ы": "и",
        "е": "е",
        "є": "е",
        "ё": "е",
        "ъ": "",
        "ь": "",
    }
)


def normalize(text: str) -> str:
    """Огрубляем строку для нечёткого сравнения названий городов.

    Приводим к нижнему регистру, схлопываем близкие буквы (і/и/ї/й, е/є)
    и выкидываем апострофы — тёзок и разночтений в украинских топонимах много.
    """
    text = text.lower().strip()
    for ch in _APOSTROPHES:
        text = text.replace(ch, "")
    return text.translate(_TRANSLATE)


def _to_float(value: Any) -> float | None:
    """Аккуратно приводим координату к float, мусор превращаем в None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if value == "":
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_coords(lat: float | None, lon: float | None) -> bool:
    """Отбраковка мусора: нули, None и «остров в Гвинейском заливе»."""
    if lat is None or lon is None:
        return False
    if lat == 0 or lon == 0:
        return False
    # Украина примерно в этих границах — грубый sanity-check.
    if not (43.0 <= lat <= 53.0 and 21.0 <= lon <= 41.0):
        return False
    return True


class Cache:
    """Обёртка над SQLite-кэшем справочников."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Cache не подключён, вызови connect()")
        return self._db

    # --- meta ------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    async def cargo_type_ref(self) -> str | None:
        return await self.get_meta("cargo_type_ref")

    async def last_sync_at(self) -> datetime | None:
        raw = await self.get_meta("last_sync_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    # --- проверка необходимости синка -----------------------------------

    async def is_empty(self) -> bool:
        async with self.db.execute("SELECT COUNT(*) AS c FROM warehouses") as cur:
            row = await cur.fetchone()
        return (row["c"] if row else 0) == 0

    async def needs_sync(self, max_age_hours: int = 24) -> bool:
        if await self.is_empty():
            return True
        last = await self.last_sync_at()
        if last is None:
            return True
        age = datetime.now(timezone.utc) - last
        return age.total_seconds() > max_age_hours * 3600

    # --- разрешение GUID грузового типа ---------------------------------

    async def resolve_cargo_type_ref(self, np: NovaPoshtaClient) -> str:
        """Находит Ref грузового отделения и кладёт в meta.

        Не хардкодим GUID: спрашиваем getWarehouseTypes и ищем «Вантажне».
        Если не нашли — падаем с внятной ошибкой, а не молча.
        """
        cached = await self.cargo_type_ref()
        if cached:
            return cached

        types = await np.get_warehouse_types()
        for t in types:
            desc = (t.get("Description") or "").lower()
            if any(kw in desc for kw in CARGO_TYPE_KEYWORDS):
                ref = t.get("Ref")
                if ref:
                    await self.set_meta("cargo_type_ref", ref)
                    logger.info(
                        "Грузовой тип отделения: %s (%s)",
                        t.get("Description"),
                        ref,
                    )
                    return ref

        raise NovaPoshtaError(
            "Не удалось найти тип «Вантажне відділення» в getWarehouseTypes — "
            "API изменил справочник типов, нужен разбор вручную"
        )

    # --- синхронизация ---------------------------------------------------

    async def sync_settlements(
        self, np: NovaPoshtaClient, limit: int = 150
    ) -> int:
        """Тянет справочник населённых пунктов постранично, батчами."""
        page = 1
        saved = 0
        while True:
            data = await np.get_settlements(page=page, limit=limit)
            if not data:
                break
            rows = list(self._settlement_rows(data))
            if rows:
                await self.db.executemany(
                    "INSERT INTO settlements "
                    "(ref, description, area, region, settlement_type, "
                    " latitude, longitude) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ref) DO UPDATE SET "
                    " description=excluded.description, area=excluded.area, "
                    " region=excluded.region, "
                    " settlement_type=excluded.settlement_type, "
                    " latitude=excluded.latitude, longitude=excluded.longitude",
                    rows,
                )
                await self.db.commit()
                saved += len(rows)
            logger.info("settlements: страница %d, всего сохранено %d", page, saved)
            if len(data) < limit:
                break
            page += 1
            await asyncio.sleep(PAGE_DELAY)
        return saved

    @staticmethod
    def _settlement_rows(
        data: Iterable[dict[str, Any]]
    ) -> Iterable[tuple[Any, ...]]:
        for item in data:
            lat = _to_float(item.get("Latitude"))
            lon = _to_float(item.get("Longitude"))
            # Населённые пункты без координат оставляем, но с NULL —
            # они пригодятся для поиска по названию; фолбэк по радиусу
            # использует только валидные координаты.
            if not _valid_coords(lat, lon):
                lat, lon = None, None
            ref = item.get("Ref")
            if not ref:
                continue
            yield (
                ref,
                item.get("Description"),
                item.get("AreaDescription"),
                item.get("RegionsDescription"),
                item.get("SettlementTypeDescription"),
                lat,
                lon,
            )

    async def sync_warehouses(
        self, np: NovaPoshtaClient, limit: int = 500
    ) -> int:
        """Тянет отделения постранично, батчами, с фильтрацией мусора."""
        page = 1
        saved = 0
        skipped = 0
        while True:
            data = await np.get_warehouses(page=page, limit=limit)
            if not data:
                break
            rows = []
            for item in data:
                row = self._warehouse_row(item)
                if row is None:
                    skipped += 1
                    continue
                rows.append(row)
            if rows:
                await self.db.executemany(
                    "INSERT INTO warehouses "
                    "(ref, number, description, short_address, city_ref, "
                    " type_ref, latitude, longitude, total_max_weight, "
                    " place_max_weight, schedule, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ref) DO UPDATE SET "
                    " number=excluded.number, description=excluded.description, "
                    " short_address=excluded.short_address, "
                    " city_ref=excluded.city_ref, type_ref=excluded.type_ref, "
                    " latitude=excluded.latitude, longitude=excluded.longitude, "
                    " total_max_weight=excluded.total_max_weight, "
                    " place_max_weight=excluded.place_max_weight, "
                    " schedule=excluded.schedule, status=excluded.status",
                    rows,
                )
                await self.db.commit()
                saved += len(rows)
            logger.info(
                "warehouses: страница %d, сохранено %d, отброшено %d",
                page,
                saved,
                skipped,
            )
            if len(data) < limit:
                break
            page += 1
            await asyncio.sleep(PAGE_DELAY)
        return saved

    @staticmethod
    def _warehouse_row(item: dict[str, Any]) -> tuple[Any, ...] | None:
        lat = _to_float(item.get("Latitude"))
        lon = _to_float(item.get("Longitude"))
        if not _valid_coords(lat, lon):
            return None
        status = item.get("WarehouseStatus")
        if status and status != WORKING_STATUS:
            return None
        ref = item.get("Ref")
        if not ref:
            return None

        schedule = item.get("Schedule")
        schedule_json = (
            json.dumps(schedule, ensure_ascii=False) if schedule else None
        )
        # Description приходит на украинском; берём русский вариант, если есть.
        description = item.get("DescriptionRu") or item.get("Description")

        return (
            ref,
            item.get("Number"),
            description,
            item.get("ShortAddress") or item.get("ShortAddressRu"),
            item.get("CityRef"),
            item.get("TypeOfWarehouse"),
            lat,
            lon,
            _to_float(item.get("TotalMaxWeightAllowed")) or 0.0,
            _to_float(item.get("PlaceMaxWeightAllowed")) or 0.0,
            schedule_json,
            status,
        )

    async def sync_all(self, np: NovaPoshtaClient) -> tuple[int, int]:
        """Полная синхронизация: тип груза + населённые пункты + отделения."""
        started = time.monotonic()
        await self.resolve_cargo_type_ref(np)
        s = await self.sync_settlements(np)
        w = await self.sync_warehouses(np)
        await self.set_meta(
            "last_sync_at", datetime.now(timezone.utc).isoformat()
        )
        logger.info(
            "Синхронизация завершена за %.1fс: %d нас.пунктов, %d отделений",
            time.monotonic() - started,
            s,
            w,
        )
        return s, w

    # --- запросы ---------------------------------------------------------

    async def find_settlements(
        self, query: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Поиск населённых пунктов по названию (LIKE + нормализация).

        Сначала пробуем прямой LIKE; результат дополнительно фильтруем по
        нормализованному совпадению, чтобы «і/и», апострофы и регистр не мешали.
        """
        like = f"%{query.strip()}%"
        async with self.db.execute(
            "SELECT * FROM settlements WHERE description LIKE ? "
            "COLLATE NOCASE ORDER BY LENGTH(description) LIMIT ?",
            (like, limit * 3),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        norm_q = normalize(query)
        # Точные (по нормализации) совпадения — вперёд.
        exact = [r for r in rows if normalize(r["description"] or "") == norm_q]
        partial = [
            r
            for r in rows
            if norm_q in normalize(r["description"] or "")
            and r not in exact
        ]
        return (exact + partial)[:limit]

    async def get_settlement(self, ref: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM settlements WHERE ref = ?", (ref,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_cargo_warehouses(
        self, city_ref: str
    ) -> list[dict[str, Any]]:
        """Грузовые отделения города (по кэшированному cargo_type_ref)."""
        type_ref = await self.cargo_type_ref()
        if not type_ref:
            return []
        async with self.db.execute(
            "SELECT * FROM warehouses WHERE city_ref = ? AND type_ref = ?",
            (city_ref, type_ref),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def settlements_with_cargo_nearby(
        self, lat: float, lon: float, radius_km: float = 50.0
    ) -> list[dict[str, Any]]:
        """Населённые пункты с грузовыми отделениями в радиусе от точки.

        Возвращает список ``{settlement, distance_km}`` по возрастанию
        расстояния. Используется как фолбэк, когда в самом городе грузового
        отделения нет.
        """
        type_ref = await self.cargo_type_ref()
        if not type_ref:
            return []
        # Города, в которых есть хотя бы одно грузовое отделение.
        async with self.db.execute(
            "SELECT DISTINCT s.* FROM settlements s "
            "JOIN warehouses w ON w.city_ref = s.ref "
            "WHERE w.type_ref = ? AND s.latitude IS NOT NULL",
            (type_ref,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        result = []
        for r in rows:
            d = haversine_km(lat, lon, r["latitude"], r["longitude"])
            if d <= radius_km:
                result.append({"settlement": r, "distance_km": d})
        result.sort(key=lambda x: x["distance_km"])
        return result

    # --- кэш геокодинга --------------------------------------------------

    async def get_geocode(self, query: str) -> tuple[float, float] | None:
        async with self.db.execute(
            "SELECT latitude, longitude FROM geocode_cache WHERE query = ?",
            (query,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return row["latitude"], row["longitude"]

    async def save_geocode(
        self, query: str, lat: float, lon: float
    ) -> None:
        await self.db.execute(
            "INSERT INTO geocode_cache (query, latitude, longitude, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(query) DO UPDATE SET "
            " latitude=excluded.latitude, longitude=excluded.longitude, "
            " created_at=excluded.created_at",
            (query, lat, lon, datetime.now(timezone.utc).isoformat()),
        )
        await self.db.commit()
