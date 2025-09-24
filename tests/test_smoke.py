from app.agents import search_assistant

def test_agent_has_name():
    assert search_assistant.name == "search_assistant"