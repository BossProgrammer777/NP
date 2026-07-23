"""Сценарий 1 — 📍 Найти отделение."""

from __future__ import annotations

import json
import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import texts
from bot import widgets
from bot.services.cache import Cache
from bot.services.geo import format_distance, haversine_km, maps_link
from bot.services.geocoder import Geocoder
from bot.states import FindWarehouse
from bot.utils import SyncState

logger = logging.getLogger(__name__)

router = Router(name="find_warehouse")

WEEKDAYS_RU = {
    "Monday": "Пн",
    "Tuesday": "Вт",
    "Wednesday": "Ср",
    "Thursday": "Чт",
    "Friday": "Пт",
    "Saturday": "Сб",
    "Sunday": "Вс",
}
NEAREST_COUNT = 3


def format_schedule(schedule_json: str | None) -> str | None:
    """Компактное расписание: пропускаем закрытые дни."""
    if not schedule_json:
        return None
    try:
        schedule = json.loads(schedule_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(schedule, dict):
        return None
    parts = []
    for eng, ru in WEEKDAYS_RU.items():
        hours = schedule.get(eng)
        if not hours or hours in ("-", "00:00-00:00"):
            continue
        parts.append(f"{ru} {hours}")
    return ", ".join(parts) if parts else None


def format_warehouse(w: dict[str, Any], distance_km: float | None = None) -> str:
    """Человекочитаемая карточка отделения."""
    lines = [f"🏢 {w.get('description') or 'Грузовое отделение'}"]
    if w.get("short_address"):
        lines.append(f"📍 {w['short_address']}")
    if distance_km is not None:
        lines.append(f"📏 {format_distance(distance_km)} от вас")

    place = w.get("place_max_weight") or 0
    total = w.get("total_max_weight") or 0
    if place or total:
        weight_parts = []
        if place:
            weight_parts.append(f"до {int(place)} кг на место")
        if total:
            weight_parts.append(f"{int(total)} кг всего")
        lines.append("⚖️ " + ", ".join(weight_parts))

    schedule = format_schedule(w.get("schedule"))
    if schedule:
        lines.append(f"🕐 {schedule}")

    if w.get("latitude") and w.get("longitude"):
        lines.append(f"🗺 {maps_link(w['latitude'], w['longitude'])}")
    return "\n".join(lines)


# --- Вход в сценарий ------------------------------------------------------


@router.message(F.text == texts.BTN_FIND_WAREHOUSE)
async def entry(
    message: Message, state: FSMContext, sync_state: SyncState
) -> None:
    if sync_state.in_progress:
        await message.answer(texts.SYNCING)
        return
    await state.clear()
    await widgets.ask_city(
        message, state, FindWarehouse.waiting_city, texts.ASK_CITY
    )


# --- Выбор города ---------------------------------------------------------


async def _city_selected(
    message: Message, state: FSMContext, settlement: dict[str, Any]
) -> None:
    """Город выбран — запоминаем и просим улицу."""
    await state.update_data(
        city_ref=settlement["ref"],
        city_name=settlement.get("description"),
        city_area=settlement.get("area"),
        city_lat=settlement.get("latitude"),
        city_lon=settlement.get("longitude"),
    )
    await state.set_state(FindWarehouse.waiting_street)
    await message.answer(texts.ASK_STREET, reply_markup=kb.cancel_menu())


@router.message(FindWarehouse.waiting_city)
async def city_text(
    message: Message, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_text(
        message, state, cache, FindWarehouse.choosing_city, _city_selected
    )


@router.callback_query(
    FindWarehouse.choosing_city, F.data.startswith(f"{kb.CB_CITY}:")
)
async def city_choice(
    callback: CallbackQuery, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_choice(callback, state, cache, _city_selected)


# --- Улица и выдача результата -------------------------------------------


@router.message(FindWarehouse.waiting_street)
async def street_text(
    message: Message,
    state: FSMContext,
    cache: Cache,
    geocoder: Geocoder,
) -> None:
    data = await state.get_data()
    city_ref = data["city_ref"]
    city_name = data.get("city_name") or ""
    city_area = data.get("city_area") or ""
    street = (message.text or "").strip()

    warehouses = await cache.get_cargo_warehouses(city_ref)

    # Фолбэк: в городе нет грузовых отделений — ищем рядом.
    if not warehouses:
        await _handle_no_cargo(message, state, cache, data)
        return

    # Геокодим адрес. Если не вышло — показываем все отделения города списком.
    query = f"Україна, {city_area}, {city_name}, {street}"
    coords = await geocoder.geocode(query)

    if coords is None:
        await message.answer(
            texts.STREET_NOT_FOUND.format(city=city_name),
            reply_markup=kb.main_menu(),
        )
        for w in warehouses:
            await message.answer(format_warehouse(w))
        await state.clear()
        return

    lat, lon = coords
    ranked = sorted(
        warehouses,
        key=lambda w: haversine_km(lat, lon, w["latitude"], w["longitude"]),
    )[:NEAREST_COUNT]

    await message.answer(
        texts.NEAREST_HEADER.format(city=city_name),
        reply_markup=kb.main_menu(),
    )
    for w in ranked:
        d = haversine_km(lat, lon, w["latitude"], w["longitude"])
        await message.answer(format_warehouse(w, distance_km=d))
    await state.clear()


async def _handle_no_cargo(
    message: Message,
    state: FSMContext,
    cache: Cache,
    data: dict[str, Any],
) -> None:
    """В городе нет грузового отделения — фолбэк по радиусу 50 км."""
    city_name = data.get("city_name") or ""
    city_lat = data.get("city_lat")
    city_lon = data.get("city_lon")

    if city_lat is None or city_lon is None:
        await message.answer(
            texts.NO_CARGO_ANYWHERE.format(city=city_name),
            reply_markup=kb.main_menu(),
        )
        await state.clear()
        return

    nearby = await cache.settlements_with_cargo_nearby(
        city_lat, city_lon, radius_km=50.0
    )
    if not nearby:
        await message.answer(
            texts.NO_CARGO_ANYWHERE.format(city=city_name),
            reply_markup=kb.main_menu(),
        )
        await state.clear()
        return

    nearest = nearby[0]
    nearest_name = nearest["settlement"].get("description") or ""
    await message.answer(
        texts.NO_CARGO_IN_CITY.format(
            city=city_name,
            nearest=nearest_name,
            distance=format_distance(nearest["distance_km"]),
        ),
        reply_markup=kb.main_menu(),
    )
    # Показываем грузовые отделения ближайшего города.
    warehouses = await cache.get_cargo_warehouses(nearest["settlement"]["ref"])
    for w in warehouses[:NEAREST_COUNT]:
        await message.answer(format_warehouse(w))
    await state.clear()
