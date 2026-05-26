from pydantic import BaseModel, Field


class Config(BaseModel):
    help_config_path: str = "data/help/help.json"
    help_commands: list[str] = Field(default_factory=lambda: ["help", "帮助"])
    help_priority: int = 10
    help_block: bool = True
