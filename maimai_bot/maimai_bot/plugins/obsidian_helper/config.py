from pydantic import BaseModel


class Config(BaseModel):
    """Configuration for the obsidian helper plugin."""

    obsidian_proxy: str = ""
    obsidian_ai_api_key: str = ""
    obsidian_ai_model: str = ""
    obsidian_ai_base_url: str = ""
    obsidian_help_base_url: str = "https://help.obsidian.md"
    obsidian_help_cache_ttl_hours: int = 24
    obsidian_help_max_candidates: int = 3
    obsidian_community_plugins_json_url: str = (
        "https://raw.githubusercontent.com/obsidianmd/obsidian-releases/master/community-plugins.json"
    )
    obsidian_community_plugins_cache_ttl_hours: int = 24
    obsidian_community_plugins_max_candidates: int = 3
