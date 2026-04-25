from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str
    anthropic_model: str = "claude-opus-4-7"

    evolution_api_url: str
    evolution_api_key: str
    evolution_instance_name: str = "obstetra"

    doctor_phone_number: str = ""
    doctor_name: str = "Dra. Leiza"

    database_url: str = "sqlite:///./data/obstetra.db"

    log_level: str = "INFO"


settings = Settings()
