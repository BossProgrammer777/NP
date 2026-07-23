"""Мелкие вспомогательные структуры, общие для хендлеров."""

from __future__ import annotations

from dataclasses import dataclass


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
