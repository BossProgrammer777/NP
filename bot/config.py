"""Конфигурация бота из переменных окружения."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения.

    Значения читаются из переменных окружения (или файла .env при локальной
    разработке). См. .env.example со списком всех переменных.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram
    bot_token: str

    # Новая Почта
    np_api_key: str

    # Путь к SQLite-файлу с кэшем справочников.
    # На Railway обязательно смонтировать Volume и указать сюда путь на нём,
    # например /data/np.db — иначе кэш будет теряться при каждом редеплое.
    db_path: str = "np.db"

    # Геокодер
    # Nominatim требует осмысленный User-Agent с контактом, иначе банит по IP.
    geocoder_user_agent: str = "np-cargo-bot/1.0 (contact: set-me@example.com)"
    # Провайдер геокодинга: пока поддержан только "nominatim".
    # Задел для подмены на "google" одной строкой в конфиге.
    geocoder_provider: str = "nominatim"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    google_geocoder_api_key: str = ""

    # Логирование
    log_level: str = "INFO"


settings = Settings()
