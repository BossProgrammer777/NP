"""Ручной запуск синхронизации справочников.

Тянет типы отделений, населённые пункты и отделения в локальный SQLite и
печатает контрольные цифры — сколько записей легло и не нулевые ли координаты.

Запуск::

    python -m scripts.sync
"""

import asyncio
import logging

from bot.config import settings
from bot.services.cache import Cache
from bot.services.novaposhta import NovaPoshtaClient


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cache = Cache(settings.db_path)
    await cache.connect()
    try:
        async with NovaPoshtaClient(settings.np_api_key) as np:
            settlements, warehouses = await cache.sync_all(np)

        print(f"\nНаселённых пунктов сохранено: {settlements}")
        print(f"Отделений (грузовых и прочих) сохранено: {warehouses}")
        print(f"Грузовые типы: {await cache.cargo_type_refs()}")

        async with cache.db.execute("SELECT COUNT(*) AS c FROM cities") as cur:
            row = await cur.fetchone()
        print(f"Городов в каталоге: {row['c']}")

        # Контроль координат.
        async with cache.db.execute(
            "SELECT COUNT(*) AS c FROM warehouses "
            "WHERE latitude IS NULL OR latitude = 0"
        ) as cur:
            row = await cur.fetchone()
        print(f"Отделений с пустыми координатами (должно быть 0): {row['c']}")

        # Проверка на живом примере: Киев ищем и по-русски.
        found = await cache.find_cities("Киев")
        print(f"\nПоиск 'Киев' -> {len(found)} совпадений:")
        for c in found[:3]:
            print(f"  {c['description']} (area={c['area']}) ref={c['ref']}")
            wh = await cache.get_cargo_warehouses(c["ref"])
            print(f"    грузовых отделений: {len(wh)}")
    finally:
        await cache.close()


if __name__ == "__main__":
    asyncio.run(main())
