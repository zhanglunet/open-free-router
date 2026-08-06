"""open-free-router — Free LLM Model Router & Pipeline

Quick start:
    pip install open-free-router
    open-free-router serve   # starts proxy (8337) + UI (9057) + scheduler (12h refresh)
    open-free-router ui      # standalone web dashboard
    open-free-router refresh # one-time free model list refresh
    open-free-router sync --agent codex  # create Codex Responses profile
    open-free-router add     # add a provider
    open-free-router mcp     # MCP stdio server for agent hosts
"""

__version__ = "0.4.0"
