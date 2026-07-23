"""Клавиатуры: главное меню и инлайн-клавиатуры."""

from __future__ import annotations

from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot import texts

# Callback-префиксы.
CB_CITY = "city"  # выбор города из списка уточнения
CB_SERVICE = "svc"  # выбор ServiceType
CB_SKIP = "skip"  # пропустить шаг (габариты)
CB_DATE = "date"  # выбор даты отправки
CB_CANCEL = "cancel"


def main_menu() -> ReplyKeyboardMarkup:
    """Главное меню — всегда доступно."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_FIND_WAREHOUSE)],
            [
                KeyboardButton(text=texts.BTN_DELIVERY_DATE),
                KeyboardButton(text=texts.BTN_PRICE),
            ],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    """Reply-клавиатура с одной кнопкой «Отмена» на время сценария."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder="Введите значение или «Отмена»",
    )


def city_choices(settlements: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Инлайн-кнопки уточнения города.

    Формат подписи: «Олександрівка, Кіровоградська обл., Кропивницький р-н».
    В callback_data кладём Ref населённого пункта.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for s in settlements:
        parts = [s.get("description") or "—"]
        if s.get("area"):
            parts.append(f"{s['area']} обл.")
        if s.get("region"):
            parts.append(f"{s['region']} р-н")
        label = ", ".join(parts)
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"{CB_CITY}:{s['ref']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_types() -> InlineKeyboardMarkup:
    """Четыре кнопки выбора ServiceType."""
    rows = [
        [
            InlineKeyboardButton(
                text=label, callback_data=f"{CB_SERVICE}:{code}"
            )
        ]
        for code, label in texts.SERVICE_TYPES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ship_date() -> InlineKeyboardMarkup:
    """Кнопки выбора даты отправки: сегодня / завтра."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_TODAY, callback_data=f"{CB_DATE}:today"
                ),
                InlineKeyboardButton(
                    text=texts.BTN_TOMORROW, callback_data=f"{CB_DATE}:tomorrow"
                ),
            ]
        ]
    )


def skip_dimensions() -> InlineKeyboardMarkup:
    """Кнопка «Пропустить» для шага габаритов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_SKIP, callback_data=f"{CB_SKIP}:dims"
                )
            ]
        ]
    )
