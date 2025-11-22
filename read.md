# my-project

A project that integrates with a GitHub-hosted toolkit using a docs contract (`docsmap.yaml`) to keep code aligned to official guidance.

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
make install
make run

ChatGPT Guiderails

Toolkit repo: https://github.com/google/adk-python (branch: main)
Toolkit documentation: https://google.github.io/adk-docs/
Project repo: https://github.com/klagster/samaia-1.1
Tech stack: Python 3.12, add-python, google cloud services
Deliverables: CLI + REST API + example notebooks + ADK test environment
Constraints: Prefer official toolkit patterns only; avoid deprecated APIs
Docs directories to trust: /docs, /examples, /reference
ADK Toolkit documentation: https://google.github.io/adk-docs/
ADK Samples: https://github.com/google/adk-samples
ADK API reference: https://google.github.io/adk-docs/api-reference/
ADK Quickstart: https://google.github.io/adk-docs/get-started/quickstart/
Must-cite: Yes—always cite file/section when recommending APIs or patterns
