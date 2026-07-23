"""Общие хендлеры: /start, /help, /cancel, возврат в меню, фолбэк."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import keyboards as kb
from bot import texts

# Роутер команд — регистрируется ПЕРВЫМ, чтобы /start, /help и /cancel
# работали в любом состоянии FSM.
router = Router(name="common")

# Отдельный роутер-фолбэк — регистрируется ПОСЛЕДНИМ, ловит всё непонятое.
fallback_router = Router(name="fallback")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.START, reply_markup=kb.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=kb.main_menu())


@router.message(Command("cancel"))
@router.message(F.text.casefold() == texts.BTN_CANCEL.casefold())
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=kb.main_menu())


@fallback_router.message()
async def unknown(message: Message) -> None:
    await message.answer(texts.UNKNOWN, reply_markup=kb.main_menu())
