"""Локальный кэш справочников Новой Почты в SQLite.

НП требует хранить копию справочников у себя и обновлять раз в сутки.
Отделений больше 50 тысяч — дёргать API на каждое сообщение нельзя.

Важно про идентификаторы НП: `getSettlements` отдаёт свой `Ref` (населённый
пункт), а у отделений (`getWarehouses`) город задаётся отдельным `CityRef`
(это ссылка на «город» в терминах доставки). Эти ключи РАЗНЫЕ. Поэтому
каталог городов для поиска мы строим из самих отделений — по их `CityRef` и
`CityDescription`/`CityDescriptionRu`. Тогда и связь город→отделения верна, и
тот же `CityRef` годится для расчёта сроков и стоимости.
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

# Версия схемы/логики кэша. При изменении — принудительный ресинк, чтобы
# перестроить таблицы на уже существующем Volume.
SCHEMA_VERSION = "2"

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
    description_ru TEXT,
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
    city_name TEXT,
    city_name_ru TEXT,
    settlement_ref TEXT,
    type_ref TEXT,
    latitude REAL,
    longitude REAL,
    total_max_weight REAL,
    place_max_weight REAL,
    schedule TEXT,
    status TEXT
);

-- Каталог городов, построенный из отделений (ключ CityRef).
CREATE TABLE IF NOT EXISTS cities (
    ref TEXT PRIMARY KEY,
    name TEXT,
    name_ru TEXT,
    area TEXT,
    settlement_ref TEXT,
    latitude REAL,
    longitude REAL
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
CREATE INDEX IF NOT EXISTS idx_cities_name ON cities (name);
CREATE INDEX IF NOT EXISTS idx_cities_name_ru ON cities (name_ru);
"""

# Миграции для уже существующей БД на Volume (ADD COLUMN игнорируем, если есть).
MIGRATIONS = (
    "ALTER TABLE settlements ADD COLUMN description_ru TEXT",
    "ALTER TABLE warehouses ADD COLUMN city_name TEXT",
    "ALTER TABLE warehouses ADD COLUMN city_name_ru TEXT",
    "ALTER TABLE warehouses ADD COLUMN settlement_ref TEXT",
    "ALTER TABLE cities ADD COLUMN settlement_ref TEXT",
)

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
        "э": "е",
        "ъ": "",
        "ь": "",
    }
)


def normalize(text: str) -> str:
    """Огрубляем строку для нечёткого сравнения названий городов.

    Приводим к нижнему регистру, схлопываем близкие буквы (і/и/ї/й, е/є/э)
    и выкидываем апострофы — тёзок и разночтений в топонимах много.
    """
    text = (text or "").lower().strip()
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
        # Миграции существующей БД: добавляем недостающие колонки.
        for stmt in MIGRATIONS:
            try:
                await self._db.execute(stmt)
            except Exception:  # noqa: BLE001 - колонка уже есть
                pass
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

    async def cargo_type_refs(self) -> list[str]:
        raw = await self.get_meta("cargo_type_refs")
        if not raw:
            single = await self.get_meta("cargo_type_ref")
            return [single] if single else []
        try:
            return list(json.loads(raw))
        except (ValueError, TypeError):
            return []

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
        async with self.db.execute("SELECT COUNT(*) AS c FROM cities") as cur:
            row = await cur.fetchone()
        return (row["c"] if row else 0) == 0

    async def needs_sync(self, max_age_hours: int = 24) -> bool:
        # Смена версии схемы => обязательный пересбор кэша.
        if await self.get_meta("schema_version") != SCHEMA_VERSION:
            return True
        if await self.is_empty():
            return True
        last = await self.last_sync_at()
        if last is None:
            return True
        age = datetime.now(timezone.utc) - last
        return age.total_seconds() > max_age_hours * 3600

    # --- разрешение GUID грузового типа ---------------------------------

    async def resolve_cargo_type_refs(self, np: NovaPoshtaClient) -> list[str]:
        """Находит все Ref грузовых отделений и кладёт в meta.

        Не хардкодим GUID: спрашиваем getWarehouseTypes и берём все записи с
        «Вантажне». Если ни одной — падаем с внятной ошибкой, а не молча.
        """
        types = await np.get_warehouse_types()
        refs = []
        for t in types:
            desc = (t.get("Description") or "").lower()
            if any(kw in desc for kw in CARGO_TYPE_KEYWORDS):
                ref = t.get("Ref")
                if ref:
                    refs.append(ref)
                    logger.info(
                        "Грузовой тип отделения: %s (%s)",
                        t.get("Description"),
                        ref,
                    )
        if not refs:
            raise NovaPoshtaError(
                "Не удалось найти тип «Вантажне відділення» в getWarehouseTypes — "
                "API изменил справочник типов, нужен разбор вручную"
            )
        await self.set_meta("cargo_type_refs", json.dumps(refs))
        await self.set_meta("cargo_type_ref", refs[0])
        return refs

    # --- синхронизация ---------------------------------------------------

    async def sync_settlements(
        self, np: NovaPoshtaClient, limit: int = 150
    ) -> int:
        """Тянет справочник населённых пунктов постранично, батчами.

        Нужен для русских названий (DescriptionRu) и областей — из него
        строим карту укр→рус при сборке каталога городов.
        """
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
                    "(ref, description, description_ru, area, region, "
                    " settlement_type, latitude, longitude) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ref) DO UPDATE SET "
                    " description=excluded.description, "
                    " description_ru=excluded.description_ru, "
                    " area=excluded.area, region=excluded.region, "
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
            if not _valid_coords(lat, lon):
                lat, lon = None, None
            ref = item.get("Ref")
            if not ref:
                continue
            yield (
                ref,
                item.get("Description"),
                item.get("DescriptionRu"),
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
                    " city_name, city_name_ru, settlement_ref, type_ref, "
                    " latitude, longitude, "
                    " total_max_weight, place_max_weight, schedule, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(ref) DO UPDATE SET "
                    " number=excluded.number, description=excluded.description, "
                    " short_address=excluded.short_address, "
                    " city_ref=excluded.city_ref, city_name=excluded.city_name, "
                    " city_name_ru=excluded.city_name_ru, "
                    " settlement_ref=excluded.settlement_ref, "
                    " type_ref=excluded.type_ref, latitude=excluded.latitude, "
                    " longitude=excluded.longitude, "
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
            item.get("ShortAddressRu") or item.get("ShortAddress"),
            item.get("CityRef"),
            item.get("CityDescription"),
            item.get("CityDescriptionRu"),
            item.get("SettlementRef"),
            item.get("TypeOfWarehouse"),
            lat,
            lon,
            _to_float(item.get("TotalMaxWeightAllowed")) or 0.0,
            _to_float(item.get("PlaceMaxWeightAllowed")) or 0.0,
            schedule_json,
            status,
        )

    async def build_cities(self) -> int:
        """Строит каталог городов из отделений (ключ — CityRef).

        Русское имя берём из отделения (CityDescriptionRu), а если его нет —
        подтягиваем по украинскому названию из settlements. Координаты города —
        усреднённые по его отделениям (для фолбэка «ближайший город»).
        """
        # Карта укр(норм) -> (рус-имя, область) из населённых пунктов.
        ru_map: dict[str, tuple[str | None, str | None]] = {}
        async with self.db.execute(
            "SELECT description, description_ru, area FROM settlements"
        ) as cur:
            async for r in cur:
                key = normalize(r["description"] or "")
                if key and key not in ru_map:
                    ru_map[key] = (r["description_ru"], r["area"])

        async with self.db.execute(
            "SELECT city_ref, "
            "       MAX(city_name) AS name, "
            "       MAX(city_name_ru) AS name_ru, "
            "       MAX(settlement_ref) AS settlement_ref, "
            "       AVG(latitude) AS lat, AVG(longitude) AS lon "
            "FROM warehouses WHERE city_ref IS NOT NULL "
            "GROUP BY city_ref"
        ) as cur:
            agg = [dict(r) for r in await cur.fetchall()]

        rows = []
        for c in agg:
            name = c["name"]
            ru_fallback, area = ru_map.get(normalize(name or ""), (None, None))
            name_ru = c["name_ru"] or ru_fallback or name
            rows.append(
                (
                    c["city_ref"],
                    name,
                    name_ru,
                    area,
                    c["settlement_ref"],
                    c["lat"],
                    c["lon"],
                )
            )

        await self.db.execute("DELETE FROM cities")
        if rows:
            await self.db.executemany(
                "INSERT INTO cities "
                "(ref, name, name_ru, area, settlement_ref, latitude, longitude) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        await self.db.commit()
        return len(rows)

    async def sync_all(self, np: NovaPoshtaClient) -> tuple[int, int]:
        """Полная синхронизация: типы груза + нас.пункты + отделения + города."""
        started = time.monotonic()
        await self.resolve_cargo_type_refs(np)
        s = await self.sync_settlements(np)
        w = await self.sync_warehouses(np)
        cities = await self.build_cities()
        await self.set_meta(
            "last_sync_at", datetime.now(timezone.utc).isoformat()
        )
        await self.set_meta("schema_version", SCHEMA_VERSION)
        logger.info(
            "Синхронизация завершена за %.1fс: %d нас.пунктов, %d отделений, "
            "%d городов",
            time.monotonic() - started,
            s,
            w,
            cities,
        )
        return s, w

    # --- запросы ---------------------------------------------------------

    @staticmethod
    def _city_out(row: dict[str, Any]) -> dict[str, Any]:
        """Единый формат города для хендлеров (description — рус-предпочтительно)."""
        return {
            "ref": row["ref"],
            "description": row.get("name_ru") or row.get("name"),
            "name": row.get("name"),
            "area": row.get("area"),
            "region": None,
            "settlement_ref": row.get("settlement_ref"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
        }

    async def find_cities(
        self, query: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Поиск городов по названию (укр или рус) с нормализацией."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        async with self.db.execute(
            "SELECT * FROM cities WHERE name LIKE ? COLLATE NOCASE "
            "OR name_ru LIKE ? COLLATE NOCASE "
            "ORDER BY LENGTH(COALESCE(name_ru, name)) LIMIT ?",
            (like, like, limit * 4),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        norm_q = normalize(q)

        def matches_exact(r: dict[str, Any]) -> bool:
            return norm_q in (
                normalize(r.get("name") or ""),
                normalize(r.get("name_ru") or ""),
            )

        exact = [r for r in rows if matches_exact(r)]
        partial = [
            r
            for r in rows
            if r not in exact
            and (
                norm_q in normalize(r.get("name") or "")
                or norm_q in normalize(r.get("name_ru") or "")
            )
        ]
        return [self._city_out(r) for r in (exact + partial)[:limit]]

    async def get_city(self, ref: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM cities WHERE ref = ?", (ref,)
        ) as cur:
            row = await cur.fetchone()
        return self._city_out(dict(row)) if row else None

    def _cargo_placeholders(self, refs: list[str]) -> str:
        return ", ".join("?" for _ in refs)

    async def get_cargo_warehouses(
        self, city_ref: str
    ) -> list[dict[str, Any]]:
        """Грузовые отделения города (по кэшированным грузовым типам)."""
        refs = await self.cargo_type_refs()
        if not refs:
            return []
        placeholders = self._cargo_placeholders(refs)
        async with self.db.execute(
            f"SELECT * FROM warehouses WHERE city_ref = ? "
            f"AND type_ref IN ({placeholders})",
            (city_ref, *refs),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def cities_with_cargo_nearby(
        self, lat: float, lon: float, radius_km: float = 50.0
    ) -> list[dict[str, Any]]:
        """Города с грузовыми отделениями в радиусе от точки.

        Возвращает ``{city, distance_km}`` по возрастанию расстояния. Фолбэк,
        когда в самом городе грузового отделения нет.
        """
        refs = await self.cargo_type_refs()
        if not refs:
            return []
        placeholders = self._cargo_placeholders(refs)
        async with self.db.execute(
            f"SELECT DISTINCT c.* FROM cities c "
            f"JOIN warehouses w ON w.city_ref = c.ref "
            f"WHERE w.type_ref IN ({placeholders}) AND c.latitude IS NOT NULL",
            tuple(refs),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        result = []
        for r in rows:
            if r["latitude"] is None or r["longitude"] is None:
                continue
            d = haversine_km(lat, lon, r["latitude"], r["longitude"])
            if d <= radius_km:
                result.append({"city": self._city_out(r), "distance_km": d})
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
