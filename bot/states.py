"""FSM-состояния всех трёх сценариев."""

from aiogram.fsm.state import State, StatesGroup


class FindWarehouse(StatesGroup):
    """Сценарий 1 — поиск отделения."""

    waiting_city = State()
    choosing_city = State()  # при неоднозначности названия
    waiting_street = State()


class DeliveryDate(StatesGroup):
    """Сценарий 2 — расчёт сроков."""

    waiting_sender_city = State()
    choosing_sender_city = State()
    waiting_recipient_city = State()
    choosing_recipient_city = State()
    waiting_service_type = State()


class Price(StatesGroup):
    """Сценарий 3 — расчёт стоимости."""

    waiting_sender_city = State()
    choosing_sender_city = State()
    waiting_recipient_city = State()
    choosing_recipient_city = State()
    waiting_service_type = State()
    waiting_weight = State()
    waiting_dimensions = State()
    waiting_declared_cost = State()
    waiting_seats = State()
