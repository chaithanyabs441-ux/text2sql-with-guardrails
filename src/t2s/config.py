from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = (
        "postgresql+psycopg2://t2s_user:t2s_password@localhost:5433/t2s_db"
    )
    DATABASE_URL_READONLY: str = (
        "postgresql+psycopg2://t2s_readonly:t2s_readonly_password@localhost:5433/t2s_db"
    )

    # Generator LLM (Defaults to Groq)
    GENERATOR_BASE_URL: str = "https://api.groq.com/openai/v1"
    GENERATOR_API_KEY: str = ""
    GENERATOR_MODEL: str = "llama-3.3-70b-versatile"

    # Judge LLM (Defaults to Groq Qwen for diversity)
    JUDGE_BASE_URL: str = "https://api.groq.com/openai/v1"
    JUDGE_API_KEY: str = ""
    JUDGE_MODEL: str = "qwen-2.5-coder-32b" # Or whatever Qwen model is available on Groq

    # Authentication cookies are HTTP-only and can be forced to HTTPS in deployments.
    AUTH_COOKIE_SECURE: bool = False

    # App Settings
    MAX_RETRIES: int = 3
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

# Singleton instance to import across the app
settings = Settings()
