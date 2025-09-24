from dotenv import load_dotenv
import asyncio
import json
import os
import sys

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

# Import the configured agent and the message builder helper
from app.agents import client_profile_agent, build_client_profile_message

APP_NAME = "client_profile_app"
USER_ID = "user_123"
SESSION_ID = "session_abc"


async def main() -> None:
    # Load .env so VERTEX_* env vars are available to ADK/google-genai
    load_dotenv()

    # Optional: sanity check Vertex config so failures are obvious
    project = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("VERTEX_LOCATION", "us-central1")
    if not project:
        print(
            "WARNING: VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT not set. "
            "Vertex will fail to initialize.",
            file=sys.stderr,
        )
    else:
        print(f"[Vertex] project={project} location={location}")

    # Session + Runner
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID
    )
    runner = Runner(agent=client_profile_agent, app_name=APP_NAME, session_service=session_service)

    # --- Sample payload to test end-to-end ---
    payload = {
        "company": "Oracle",
        "website": "https://www.oracle.com/",
        "doc_urls": [],
    }

    # Use the helper to turn the payload into a Content message for the Runner
    content = build_client_profile_message(payload)

    final_text = None
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=SESSION_ID,
        new_message=content,
    ):
        if event.is_final_response():
            # ADK events carry a Content object; flatten any text parts
            if event.content and getattr(event.content, "parts", None):
                texts = [getattr(p, "text", "") for p in event.content.parts if getattr(p, "text", None)]
                final_text = "".join(texts) if texts else None

    if final_text:
        # Pretty-print JSON if the model returned JSON; otherwise dump raw text
        try:
            parsed = json.loads(final_text)
            print(json.dumps(parsed, indent=2))
        except Exception:
            print(final_text)
    else:
        print("No final response text returned.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())