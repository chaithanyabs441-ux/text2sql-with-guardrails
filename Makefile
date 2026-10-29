.DEFAULT_GOAL := up

COMPOSE := docker compose
ENV_FILE := .env

.PHONY: up start rebuild down stop restart status logs follow setup check-env check-docker migrate bootstrap-admin test lint clean

# `make` starts the complete application in the background, reusing built images.
up: check-docker check-env
	$(COMPOSE) up -d
	@printf '\nSafeQuery AI is starting.\nOpen in your browser: \033]8;;http://localhost:8501\033\\http://localhost:8501\033]8;;\033\\\nAPI docs:             http://localhost:8000/docs\nHealth:               http://localhost:8000/health\n\nUse `make logs` to follow startup, or `make status` to check services.\n'

# Rebuild images after changing application code or dependencies.
rebuild: check-docker check-env
	$(COMPOSE) up --build -d
	@printf '\nSafeQuery AI is rebuilding and starting.\nOpen in your browser: \033]8;;http://localhost:8501\033\\http://localhost:8501\033]8;;\033\\\nAPI docs:             http://localhost:8000/docs\nHealth:               http://localhost:8000/health\n\nUse `make logs` to follow startup, or `make status` to check services.\n'

start: up

# Copy the template once; edit .env and add provider keys before running `make`.
setup:
	@if [ -e "$(ENV_FILE)" ]; then \
		echo "$(ENV_FILE) already exists; leaving it unchanged."; \
	else \
		cp .env.example "$(ENV_FILE)"; \
		echo "Created $(ENV_FILE) from .env.example. Add GENERATOR_API_KEY and JUDGE_API_KEY, then run make."; \
	fi

check-docker:
	@command -v docker >/dev/null 2>&1 || { echo "Docker is required. Install/start Docker Desktop or Docker Engine."; exit 1; }
	@docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is required (the 'docker compose' command)."; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "Docker is installed but its daemon is not running. Start Docker, then retry."; exit 1; }

check-env:
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo "Missing $(ENV_FILE). Run 'make setup', add GENERATOR_API_KEY and JUDGE_API_KEY, then run make."; exit 1; \
	fi
	@for key in GENERATOR_API_KEY JUDGE_API_KEY; do \
		value=$$(sed -n "s/^$$key=//p" "$(ENV_FILE)" | tail -n 1); \
		if [ -z "$$value" ] || [ "$$value" = "replace-with-generator-api-key" ] || [ "$$value" = "replace-with-judge-api-key" ]; then \
			echo "Set a real $$key in $(ENV_FILE) before starting the app."; exit 1; \
		fi; \
	done

status:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs --tail=100

follow:
	$(COMPOSE) logs --follow

down stop:
	$(COMPOSE) down

restart: down up

migrate:
	$(COMPOSE) exec api python -m t2s.auth.migrate

# Export BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD in your shell first.
bootstrap-admin:
	@test -n "$$BOOTSTRAP_ADMIN_USERNAME" -a -n "$$BOOTSTRAP_ADMIN_PASSWORD" || { echo "Export BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD first."; exit 1; }
	$(COMPOSE) exec -e BOOTSTRAP_ADMIN_USERNAME -e BOOTSTRAP_ADMIN_PASSWORD api python -m t2s.auth.bootstrap

test:
	python -m pytest

lint:
	python -m ruff check .

clean:
	$(COMPOSE) down --remove-orphans
