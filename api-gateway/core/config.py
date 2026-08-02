"""Configuration settings for AM API Gateway v2.0"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    All service URLs are injected as environment variables by Helm/Vault.
    The registry in main.py reads directly from os.getenv() so that SERVICES_REGISTRY
    can be updated without touching config.py (open-closed principle).
    """

    # Auth service (am-auth-tokens)
    AUTH_SERVICE_URL: str = "http://am-auth-tokens:8080"

    # Rate limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    # Timeouts (seconds)
    DEFAULT_TIMEOUT: float = 30.0
    LONG_TIMEOUT: float = 60.0

    # Logging
    LOG_FORMAT: str = "json"
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
