"""Точка входа: регистрация роутеров, старт long polling.

Сервис разворачивается на Railway как worker (long polling, без webhook,
без биндинга $PORT). Только одна реплика — иначе Telegram отдаёт 409 Conflict.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import settings
from bot.handlers import common, find_warehouse
from bot.services.cache import Cache
from bot.services.geocoder import Geocoder, build_geocoder
from bot.services.novaposhta import NovaPoshtaClient
from bot.utils import SyncState

logger = logging.getLogger(__name__)


async def run_sync(cache: Cache, sync_state: SyncState) -> None:
    """Разовая синхронизация справочников с флагом in_progress."""
    sync_state.in_progress = True
    try:
        async with NovaPoshtaClient(settings.np_api_key) as np:
            await cache.sync_all(np)
    except Exception:  # noqa: BLE001 - логируем и не роняем бота
        logger.exception("Синхронизация справочников не удалась")
    finally:
        sync_state.in_progress = False


async def on_startup(
    cache: Cache, sync_state: SyncState, scheduler: AsyncIOScheduler
) -> None:
    """Стартовая логика: синк при необходимости + ежедневное расписание."""
    await cache.connect()

    if await cache.needs_sync():
        logger.info("Справочники устарели или пусты — запускаю синхронизацию")
        # В фоне, чтобы бот сразу начал отвечать (пусть и «идёт обновление»).
        asyncio.create_task(run_sync(cache, sync_state))
    else:
        logger.info("Справочники свежие, синхронизация не нужна")

    # Ежедневное обновление в 4:00.
    scheduler.add_job(
        run_sync,
        trigger="cron",
        hour=4,
        minute=0,
        args=[cache, sync_state],
        id="daily_sync",
        replace_existing=True,
    )
    scheduler.start()


async def on_shutdown(
    cache: Cache, geocoder: Geocoder, scheduler: AsyncIOScheduler
) -> None:
    """Аккуратное закрытие ресурсов по SIGTERM (Railway шлёт его при редеплое)."""
    logger.info("Останавливаюсь, закрываю ресурсы")
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await geocoder.aclose()
    await cache.close()


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    cache = Cache(settings.db_path)
    geocoder = build_geocoder(cache, settings)
    sync_state = SyncState()
    scheduler = AsyncIOScheduler(timezone="Europe/Kiev")

    # Зависимости прокидываем в хендлеры через workflow_data.
    dp["cache"] = cache
    dp["geocoder"] = geocoder
    dp["sync_state"] = sync_state

    # Порядок регистрации важен: команды первыми, фолбэк последним.
    dp.include_router(common.router)
    dp.include_router(find_warehouse.router)
    dp.include_router(common.fallback_router)

    await on_startup(cache, sync_state, scheduler)
    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown(cache, geocoder, scheduler)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
