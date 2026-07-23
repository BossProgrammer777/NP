"""Дымовой тест клиента НП.

Дёргает getWarehouseTypes и searchSettlements и печатает сырой ответ —
чтобы убедиться, что API-ключ рабочий и структура ответа такая, как ожидаем.

Запуск::

    python -m scripts.smoke_test
"""

import asyncio
import json
import logging

from bot.config import settings
from bot.services.novaposhta import NovaPoshtaClient, NovaPoshtaError


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    async with NovaPoshtaClient(settings.np_api_key) as np:
        print("=== getWarehouseTypes ===")
        try:
            types = await np.get_warehouse_types()
            print(json.dumps(types, ensure_ascii=False, indent=2))
            print(f"\nВсего типов отделений: {len(types)}")
            cargo = [
                t
                for t in types
                if "вантаж" in (t.get("Description", "")).lower()
            ]
            print("Похоже на грузовое отделение:")
            print(json.dumps(cargo, ensure_ascii=False, indent=2))
        except NovaPoshtaError as exc:
            print(f"Ошибка API: {exc}")

        print("\n=== searchSettlements(Київ) ===")
        try:
            found = await np.search_settlements("Київ", limit=5)
            print(json.dumps(found, ensure_ascii=False, indent=2))
        except NovaPoshtaError as exc:
            print(f"Ошибка API: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
