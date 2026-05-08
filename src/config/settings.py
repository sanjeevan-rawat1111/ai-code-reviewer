"""Pydantic settings — all config via env vars or .env file.

Prefix convention:  REVIEWER_<SECTION>__<KEY>
Examples:
  REVIEWER_LLM__MODEL=gpt-4o
  REVIEWER_GITHUB__TOKEN=ghp_...
  REVIEWER_GITLAB__BASE_URL=https://gitlab.example.com
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEWER_LLM__", extra="ignore")

    model: str = "gemini-1.5-flash"
    api_key: str = Field(default="", description="OpenAI-compatible API key")
    base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        description="LLM endpoint — defaults to Google AI Studio (free)",
    )
    max_tokens: int = 4096
    temperature: float = 0.2
    # Failover: used on 429 / 5xx after primary retries exhausted
    fallback_model: str = ""
    fallback_api_key: str = ""
    fallback_base_url: str = ""
    max_retries: int = 3


class GitHubSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEWER_GITHUB__", extra="ignore")

    token: str = Field(default="", description="GitHub personal access token (ghp_...)")
    base_url: str = "https://api.github.com"


class GitLabSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEWER_GITLAB__", extra="ignore")

    token: str = Field(default="", description="GitLab personal access token (glpat-...)")
    base_url: str = "https://gitlab.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="REVIEWER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    llm: LLMSettings = LLMSettings()
    github: GitHubSettings = GitHubSettings()
    gitlab: GitLabSettings = GitLabSettings()

    output_dir: str = Field(default="reports", description="Directory for saved review reports")
    max_diff_lines: int = Field(
        default=2000,
        description="Truncate diffs longer than this to fit context window",
    )
    server_port: int = 8090
    server_host: str = "0.0.0.0"
    mcp_url: str = Field(
        default="",
        description=(
            "MCP server SSE URL for codebase cross-referencing. "
            "When set, the reviewer performs a 2-phase review: first identifies key symbols, "
            "then queries the MCP server for their definitions/references before finalising. "
            "Example: http://localhost:8091/mcp/sse"
        ),
    )


# Singleton — import and reuse everywhere
settings = Settings()
