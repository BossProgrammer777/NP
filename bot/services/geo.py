"""Гео-утилиты: расстояние по haversine и его форматирование."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Расстояние между двумя точками на сфере (в километрах)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def format_distance(km: float) -> str:
    """Человекочитаемое расстояние: метры до 1 км, иначе километры."""
    if km < 1:
        return f"~{int(round(km * 1000))} м"
    if km < 10:
        return f"~{km:.1f} км"
    return f"~{int(round(km))} км"


def maps_link(lat: float, lon: float) -> str:
    """Ссылка на точку в Google Maps."""
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
