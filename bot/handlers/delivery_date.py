"""Сценарий 2 — ⏱ Просчитать сроки."""

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
from bot.states import DeliveryDate
from bot.utils import SyncState

logger = logging.getLogger(__name__)

router = Router(name="delivery_date")


@router.message(F.text == texts.BTN_DELIVERY_DATE)
async def entry(
    message: Message, state: FSMContext, sync_state: SyncState
) -> None:
    if sync_state.in_progress:
        await message.answer(texts.SYNCING)
        return
    await state.clear()
    await widgets.ask_city(
        message, state, DeliveryDate.waiting_sender_city, texts.ASK_SENDER_CITY
    )


# --- Города (переиспользуем общий виджет) --------------------------------


async def _sender_selected(
    message: Message, state: FSMContext, settlement: dict[str, Any]
) -> None:
    await state.update_data(
        sender_ref=settlement["ref"], sender_name=settlement.get("description")
    )
    await widgets.ask_city(
        message,
        state,
        DeliveryDate.waiting_recipient_city,
        texts.ASK_RECIPIENT_CITY,
    )


async def _recipient_selected(
    message: Message, state: FSMContext, settlement: dict[str, Any]
) -> None:
    await state.update_data(
        recipient_ref=settlement["ref"],
        recipient_name=settlement.get("description"),
    )
    await state.set_state(DeliveryDate.waiting_service_type)
    await message.answer(
        texts.ASK_SERVICE_TYPE, reply_markup=kb.service_types()
    )


@router.message(DeliveryDate.waiting_sender_city)
async def sender_text(
    message: Message, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_text(
        message, state, cache, DeliveryDate.choosing_sender_city, _sender_selected
    )


@router.callback_query(
    DeliveryDate.choosing_sender_city, F.data.startswith(f"{kb.CB_CITY}:")
)
async def sender_choice(
    callback: CallbackQuery, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_choice(callback, state, cache, _sender_selected)


@router.message(DeliveryDate.waiting_recipient_city)
async def recipient_text(
    message: Message, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_text(
        message,
        state,
        cache,
        DeliveryDate.choosing_recipient_city,
        _recipient_selected,
    )


@router.callback_query(
    DeliveryDate.choosing_recipient_city, F.data.startswith(f"{kb.CB_CITY}:")
)
async def recipient_choice(
    callback: CallbackQuery, state: FSMContext, cache: Cache
) -> None:
    await widgets.process_city_choice(
        callback, state, cache, _recipient_selected
    )


# --- Тип доставки и результат --------------------------------------------


@router.callback_query(
    DeliveryDate.waiting_service_type, F.data.startswith(f"{kb.CB_SERVICE}:")
)
async def service_selected(
    callback: CallbackQuery,
    state: FSMContext,
    np: NovaPoshtaClient,
) -> None:
    await callback.answer()
    service_type = (callback.data or "").split(":", 1)[-1]
    data = await state.get_data()

    try:
        result = await np.get_document_delivery_date(
            city_sender=data["sender_ref"],
            city_recipient=data["recipient_ref"],
            service_type=service_type,
            date_time=utils.today_ddmmyyyy(),
        )
    except NovaPoshtaError as exc:
        await callback.message.answer(
            texts.API_ERROR.format(error=exc), reply_markup=kb.main_menu()
        )
        await state.clear()
        return

    target = utils.extract_delivery_date(result)
    if target is None:
        await callback.message.answer(
            texts.GENERIC_ERROR, reply_markup=kb.main_menu()
        )
        await state.clear()
        return

    lines = [
        f"⏱ {data.get('sender_name')} → {data.get('recipient_name')}",
        f"📅 Ориентировочная доставка: {utils.format_delivery_line(target)}",
        texts.DELIVERY_DATE_DISCLAIMER,
    ]
    await callback.message.answer(
        "\n".join(lines), reply_markup=kb.main_menu()
    )
    await state.clear()
