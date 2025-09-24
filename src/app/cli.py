# src/app/cli.py
import typer
from app.agents import search_assistant

app = typer.Typer()

@app.command()
def ask(q: str):
  resp = search_assistant.run(q)   # basic synchronous run
  print(resp.text if hasattr(resp, "text") else resp)

if __name__ == "__main__":
  app()