"""
Configuration management for Feishu Rename Bot.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Bot settings loaded from environment / .env file."""

    # Feishu app credentials
    app_id: str
    app_secret: str
    encrypt_key: str = ""
    verification_token: str = ""

    # Target cloud-docs folder token (provided by user)
    folder_token: str = ""

    # Optional: OpenAI API key for Vision-based image recognition
    openai_api_key: str = ""

    # Bot behaviour
    command_prefix: str = "重命名"
    pending_timeout: int = 300  # seconds
    output_dir: str = "./output"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
