"""Сценарий 3 — 💰 Просчитать стоимость."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import texts
from bot import utils
from bot import widgets
from bot.services.cache import Cache
from bot.services.novaposhta import NovaPoshtaClient, NovaPoshtaError
from bot.states import Price
from bot.utils import SyncState

logger = logging.getLogger(__name__)

router = Router(name="price")

CARGO_TYPE = "Cargo"


@router.message(F.text == texts.BTN_PRICE)
async def entry(
    message: Message, state: FSMContext, sync_state: SyncState
) -> None:
    if sync_state.in_progress:
        await message.answer(texts.SYNCING)
        return
    await state.clear()
    await widgets.ask_city(
        message, state, Price.waiting_sender_city, texts.ASK_SENDER_CITY
    )


# --- Города --------------------------------------------------------------


async def _sender_selected(
    message: Message, state: FSMContext, settlement: dict[str, Any]
) -> None:
    await state.update_data(
        sender_ref=settlement["ref"], sender_name=settlement.get("description")
    )
    await widgets.ask_city(
        message, state, Price.waiting_recipient_city, texts.ASK_RECIPIENT_CITY
    )


async def _recipient_selected(
    message: Message, state: FSMContext, settlement: dict[str, Any]
) -> None:
    await state.update_data(
        recipient_ref=settlement["ref"],
        recipient_name=settlement.get("description"),
    )
    await state.set_state(Price.waiting_service_type)
    await message.answer(
        texts.ASK_SERVICE_TYPE, reply_markup=kb.service_types()
    )


@router.message(Price.waiting_sender_city)
async def sender_text(
    message: Message, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_text(
        message, state, cache, Price.choosing_sender_city, _sender_selected
    )


@router.callback_query(
    Price.choosing_sender_city, F.data.startswith(f"{kb.CB_CITY}:")
)
async def sender_choice(
    callback: CallbackQuery, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_choice(callback, state, cache, _sender_selected)


@router.message(Price.waiting_recipient_city)
async def recipient_text(
    message: Message, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_text(
        message, state, cache, Price.choosing_recipient_city, _recipient_selected
    )


@router.callback_query(
    Price.choosing_recipient_city, F.data.startswith(f"{kb.CB_CITY}:")
)
async def recipient_choice(
    callback: CallbackQuery, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_choice(
        callback, state, cache, _recipient_selected
    )


# --- Тип доставки -> вес -> габариты -> стоимость -> места ----------------


@router.callback_query(
    Price.waiting_service_type, F.data.startswith(f"{kb.CB_SERVICE}:")
)
async def service_selected(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    service_type = (callback.data or "").split(":", 1)[-1]
    await state.update_data(service_type=service_type)
    await state.set_state(Price.waiting_date)
    await callback.message.answer(
        texts.ASK_SHIP_DATE, reply_markup=kb.ship_date()
    )


async def _date_set(message: Message, state: FSMContext, date_time: str) -> None:
    await state.update_data(date_time=date_time)
    await state.set_state(Price.waiting_weight)
    await message.answer(texts.ASK_WEIGHT, reply_markup=kb.cancel_menu())


@router.callback_query(
    Price.waiting_date, F.data.startswith(f"{kb.CB_DATE}:")
)
async def date_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    kind = (callback.data or "").split(":", 1)[-1]
    date_time = (
        utils.tomorrow_ddmmyyyy() if kind == "tomorrow" else utils.today_ddmmyyyy()
    )
    await _date_set(callback.message, state, date_time)


@router.message(Price.waiting_date)
async def date_text(message: Message, state: FSMContext) -> None:
    date_time = utils.parse_ship_date(message.text or "")
    if date_time is None:
        await message.answer(texts.BAD_DATE, reply_markup=kb.ship_date())
        return
    await _date_set(message, state, date_time)


@router.message(Price.waiting_weight)
async def weight_text(message: Message, state: FSMContext) -> None:
    weight = utils.parse_weight(message.text or "")
    if weight is None:
        await message.answer(texts.BAD_WEIGHT, reply_markup=kb.cancel_menu())
        return
    await state.update_data(weight=weight)
    await state.set_state(Price.waiting_dimensions)
    await message.answer(texts.ASK_DIMENSIONS, reply_markup=kb.skip_dimensions())


@router.message(Price.waiting_dimensions)
async def dimensions_text(message: Message, state: FSMContext) -> None:
    dims = utils.parse_dimensions(message.text or "")
    if dims is None:
        await message.answer(
            texts.BAD_DIMENSIONS, reply_markup=kb.skip_dimensions()
        )
        return
    await state.update_data(dims=list(dims))
    await state.set_state(Price.waiting_declared_cost)
    await message.answer(texts.ASK_DECLARED_COST, reply_markup=kb.cancel_menu())


@router.callback_query(
    Price.waiting_dimensions, F.data.startswith(f"{kb.CB_SKIP}:")
)
async def dimensions_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(dims=None)
    await state.set_state(Price.waiting_declared_cost)
    await callback.message.answer(
        texts.ASK_DECLARED_COST, reply_markup=kb.cancel_menu()
    )


@router.message(Price.waiting_declared_cost)
async def declared_cost_text(message: Message, state: FSMContext) -> None:
    cost = utils.parse_weight(message.text or "")  # то же правило: число > 0
    if cost is None:
        await message.answer(texts.BAD_COST, reply_markup=kb.cancel_menu())
        return
    await state.update_data(declared_cost=cost)
    await state.set_state(Price.waiting_seats)
    await message.answer(texts.ASK_SEATS, reply_markup=kb.cancel_menu())


@router.message(Price.waiting_seats)
async def seats_text(
    message: Message, state: FSMContext, np: NovaPoshtaClient
) -> None:
    seats = utils.parse_int(message.text or "")
    if seats is None:
        await message.answer(texts.BAD_SEATS, reply_markup=kb.cancel_menu())
        return
    await state.update_data(seats=seats)
    await _calculate_and_reply(message, state, np)


async def _calculate_and_reply(
    message: Message, state: FSMContext, np: NovaPoshtaClient
) -> None:
    data = await state.get_data()
    weight = float(data["weight"])
    seats = int(data["seats"])
    dims = tuple(data["dims"]) if data.get("dims") else None
    declared_cost = float(data["declared_cost"])
    service_type = data["service_type"]

    options_seat = utils.build_options_seat(dims, weight, seats)

    try:
        price_data = await np.get_document_price(
            city_sender=data["sender_ref"],
            city_recipient=data["recipient_ref"],
            weight=weight,
            cost=declared_cost,
            service_type=service_type,
            cargo_type=CARGO_TYPE,
            seats_amount=seats,
            options_seat=options_seat,
        )
    except NovaPoshtaError as exc:
        await message.answer(
            texts.API_ERROR.format(error=exc), reply_markup=kb.main_menu()
        )
        await state.clear()
        return

    if not price_data:
        await message.answer(texts.GENERIC_ERROR, reply_markup=kb.main_menu())
        await state.clear()
        return

    cost = price_data[0].get("Cost")

    # Срок доставки — сразу в том же ответе, чтобы не гонять по двум сценариям.
    delivery_line = None
    try:
        date_data = await np.get_document_delivery_date(
            city_sender=data["sender_ref"],
            city_recipient=data["recipient_ref"],
            service_type=service_type,
            date_time=data.get("date_time") or utils.today_ddmmyyyy(),
        )
        target = utils.extract_delivery_date(date_data)
        if target is not None:
            delivery_line = utils.format_delivery_line(target)
    except NovaPoshtaError:
        logger.warning("Не удалось получить срок доставки для расчёта цены")

    # Расчётный вес: больший из фактического и объёмного.
    lines = [f"💰 Стоимость: {cost} грн"]
    if delivery_line:
        lines.append(f"📅 Ориентировочная доставка: {delivery_line}")

    if dims is not None:
        vol = utils.volumetric_weight_kg(dims[0], dims[1], dims[2], seats)
        chargeable = max(weight, vol)
        label = "объёмный" if vol > weight else "фактический"
        lines.append(f"📦 Расчётный вес: {round(chargeable, 1)} кг ({label})")
    else:
        lines.append(f"📦 Вес: {round(weight, 1)} кг")
        lines.append(texts.VOLUME_WEIGHT_SKIPPED)

    lines.append(texts.PRICE_DISCLAIMER)
    await message.answer("\n".join(lines), reply_markup=kb.main_menu())
    await state.clear()
