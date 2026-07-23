"""Мелкие вспомогательные структуры, общие для хендлеров."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# Коэффициент объёмного веса НП для грузов: 1 м³ ≈ 250 кг.
VOLUMETRIC_COEFFICIENT = 250.0


@dataclass
class SyncState:
    """Флаг «идёт синхронизация справочников».

    Пока первая (долгая) синхронизация не завершилась, сценарии отвечают
    пользователю «Обновляю справочники», а не молчат и не падают.
    """

    in_progress: bool = False


def parse_weight(text: str) -> float | None:
    """Парсит вес в кг: принимает и точку, и запятую как разделитель."""
    text = (text or "").strip().replace(",", ".")
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def parse_int(text: str) -> int | None:
    """Парсит целое положительное число (количество мест)."""
    try:
        value = int((text or "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_dimensions(text: str) -> tuple[float, float, float] | None:
    """Парсит габариты ДxШxВ в см. Разделитель — x/х/×/*, точка или запятая."""
    raw = (text or "").strip().lower()
    for sep in ("×", "х", "*"):  # латинский x оставляем как основной
        raw = raw.replace(sep, "x")
    parts = [p.strip().replace(",", ".") for p in raw.split("x") if p.strip()]
    if len(parts) != 3:
        return None
    try:
        d, w, h = (float(p) for p in parts)
    except (TypeError, ValueError):
        return None
    if d <= 0 or w <= 0 or h <= 0:
        return None
    return d, w, h


def today_ddmmyyyy() -> str:
    """Сегодняшняя дата в формате dd.mm.yyyy (как ждёт API НП)."""
    return date.today().strftime("%d.%m.%Y")


def extract_delivery_date(data: list[dict[str, Any]]) -> date | None:
    """Достаёт дату доставки из ответа НП.

    Поле DeliveryDate приходит вложенным объектом PHP-DateTime
    ({"date": "2026-07-25 00:00:00.000000", ...}), иногда строкой.
    """
    if not data:
        return None
    raw = data[0].get("DeliveryDate")
    if isinstance(raw, dict):
        raw = raw.get("date")
    if not raw or not isinstance(raw, str):
        return None
    text = raw.split(".")[0].strip()  # отрезаем микросекунды
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_delivery_line(target: date) -> str:
    """«25.07.2026 (через 2 дня)» с корректным склонением дней."""
    days = (target - date.today()).days
    return f"{target.strftime('%d.%m.%Y')} ({humanize_days(days)})"


def humanize_days(days: int) -> str:
    """Человеко-читаемое «через N дней» с русским склонением."""
    if days <= 0:
        return "сегодня" if days == 0 else "уже сейчас"
    if days == 1:
        return "завтра"
    tail = days % 10
    tens = days % 100
    if 11 <= tens <= 14:
        word = "дней"
    elif tail in (2, 3, 4):
        word = "дня"
    elif tail == 1:
        word = "день"
    else:
        word = "дней"
    return f"через {days} {word}"


def volumetric_weight_kg(
    length_cm: float,
    width_cm: float,
    height_cm: float,
    seats: int,
) -> float:
    """Объёмный вес всех мест по габаритам (см) одного места."""
    volume_m3 = (length_cm * width_cm * height_cm) / 1_000_000.0
    return volume_m3 * VOLUMETRIC_COEFFICIENT * seats


def build_options_seat(
    dims: tuple[float, float, float] | None,
    weight_kg: float,
    seats: int,
) -> list[dict[str, Any]] | None:
    """Массив OptionsSeat с габаритами каждого места.

    Без него НП занижает расчёт (не учитывает объёмный вес). Габариты
    считаем одинаковыми для всех мест, вес делим поровну.
    """
    if dims is None or seats <= 0:
        return None
    length, width, height = dims
    per_seat_weight = round(weight_kg / seats, 3)
    volume_m3 = round((length * width * height) / 1_000_000.0, 4)
    return [
        {
            "volumetricVolume": volume_m3,
            "volumetricWidth": width,
            "volumetricLength": length,
            "volumetricHeight": height,
            "weight": per_seat_weight,
        }
        for _ in range(seats)
    ]
