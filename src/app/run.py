import os
from dotenv import load_dotenv
from app.agents import search_assistant

def main():
    load_dotenv()
    # Simple loop to try your agent
    print("Type a question (or 'quit'):")
    while True:
        q = input("> ").strip()
        if q.lower() in {"q", "quit", "exit"}:
            break
        result = search_assistant.run(q)
        # ADK returns structured output; print the text part
        print(result.text if hasattr(result, "text") else result)

if __name__ == "__main__":
    main()