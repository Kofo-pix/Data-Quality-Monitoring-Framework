PY ?= python3
export PYTHONPATH := src

.PHONY: demo test lint profile clean docker

demo:      ## 3-day degradation demo -> demo_output/report_day{1,2,3}.html
	$(PY) -m dqmon demo
test:
	$(PY) -m pytest -q
lint:
	ruff check src tests
profile:   ## generate a starter suite from the demo database
	$(PY) -m dqmon profile --db demo_output/source.db --out suites/starter.json
clean:
	rm -rf demo_output dq_history.db .pytest_cache
docker:
	docker build -t dqmon . && docker run --rm -v $$(pwd)/demo_output:/app/demo_output dqmon
