from pydantic import BaseModel


class Config(BaseModel):
    """Plugin Config"""
    deadline_data_path: str = "./data/deadline/tasks.json"
    deadline_check_interval: int = 60  # seconds
