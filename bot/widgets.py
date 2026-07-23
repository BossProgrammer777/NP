"""Переиспользуемый виджет выбора города.

Единая логика поиска населённого пункта в кэше и уточнения при
неоднозначности. Используется во всех трёх сценариях, чтобы не копипастить.

Каждый сценарий передаёт колбэк ``on_selected(target_message, state,
settlement)``, который двигает именно его FSM дальше после того, как город
окончательно выбран.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import texts
from bot.services.cache import Cache

# Тип колбэка продолжения сценария после выбора города.
OnSelected = Callable[[Message, FSMContext, dict[str, Any]], Awaitable[None]]

LOOKUP_NONE = "none"
LOOKUP_ONE = "one"
LOOKUP_MANY = "many"


async def lookup_city(cache: Cache, query: str) -> tuple[str, Any]:
    """Ищет город в кэше. Возвращает ('none'|'one'|'many', payload)."""
    query = (query or "").strip()
    if not query:
        return LOOKUP_NONE, None
    results = await cache.find_cities(query)
    if not results:
        return LOOKUP_NONE, None
    if len(results) == 1:
        return LOOKUP_ONE, results[0]
    return LOOKUP_MANY, results


async def ask_city(
    message: Message,
    state: FSMContext,
    waiting_state: State,
    ask_text: str,
) -> None:
    """Спрашивает название города и переводит FSM в состояние ожидания."""
    await state.set_state(waiting_state)
    await message.answer(ask_text, reply_markup=kb.cancel_menu())


async def process_city_text(
    message: Message,
    state: FSMContext,
    cache: Cache,
    choosing_state: State,
    on_selected: OnSelected,
) -> None:
    """Обрабатывает введённое название города.

    - не нашли — просим ввести ещё раз;
    - один вариант — сразу вызываем on_selected;
    - несколько — показываем инлайн-уточнение и переводим в choosing_state.
    """
    status, payload = await lookup_city(cache, message.text or "")

    if status == LOOKUP_NONE:
        await message.answer(texts.CITY_NOT_FOUND, reply_markup=kb.cancel_menu())
        return

    if status == LOOKUP_ONE:
        await on_selected(message, state, payload)
        return

    # Несколько тёзок — обязательно уточняем, иначе бот будет врать.
    await state.set_state(choosing_state)
    await message.answer(
        texts.CHOOSE_CITY, reply_markup=kb.city_choices(payload)
    )


async def process_city_choice(
    callback: CallbackQuery,
    state: FSMContext,
    cache: Cache,
    on_selected: OnSelected,
) -> None:
    """Обрабатывает нажатие на инлайн-кнопку конкретного города."""
    await callback.answer()
    ref = (callback.data or "").split(":", 1)[-1]
    settlement = await cache.get_city(ref)
    if settlement is None:
        await callback.message.answer(
            texts.CITY_NOT_FOUND, reply_markup=kb.cancel_menu()
        )
        return
    # Убираем инлайн-клавиатуру у сообщения с вариантами.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - косметика, не критично
        pass
    await on_selected(callback.message, state, settlement)
