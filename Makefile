install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

lint:
	ruff check src tests
	mypy src tests

format:
	black src tests

test:
	pytest -q

run:
	python -m src.app.run

api:
	uvicorn src.app.api:app --reload