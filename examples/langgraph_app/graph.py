from mcp_server_worldquantbrain.integrations.langgraph import create_brain_agent

# Built once at import so every run shares one HTTP client and simulation limit. LangGraph Platform
# supplies the checkpointer.
graph = create_brain_agent("anthropic:claude-sonnet-5", checkpointer=None)
