.PHONY: help install dev up down logs test test-integration test-unit lint format clean

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

venv: ## Create virtual environment
	@if [ ! -d .venv ]; then \
		echo "Creating virtual environment..."; \
		uv venv; \
		echo "✅ Virtual environment created at .venv"; \
	else \
		echo "✅ Virtual environment already exists at .venv"; \
	fi

activate: ## Show how to activate virtual environment
	source .venv/bin/activate

install: venv ## Install dependencies with uv (creates venv if needed)
	uv pip install -e ".[dev]"

dev: ## Start development server with hot reload
	docker compose up app

up: ## Start all Docker services (postgres and app)
	docker compose up -d

down: ## Stop all Docker services
	docker compose down

logs: ## View logs from all services
	docker compose logs -f

logs-app: ## View logs from app service
	docker compose logs -f app

test: ## Run all tests with coverage
	pytest --cov=submissions_checker --cov-report=term-missing --cov-report=html

test-integration: ## Run integration tests only
	pytest tests/integration/ -v

test-unit: ## Run unit tests only
	pytest tests/unit/ -v

test-watch: ## Run tests in watch mode
	pytest --watch

db-shell: ## Open PostgreSQL shell
	docker compose exec postgres psql -U postgres -d submissions_checker

lint: ## Run Ruff linting
	ruff check src/ tests/

lint-fix: ## Run Ruff linting with auto-fix
	ruff check --fix src/ tests/

format: ## Format code with Ruff
	ruff format src/ tests/

format-check: ## Check code formatting without modifying files
	ruff format --check src/ tests/

type-check: ## Run mypy type checking
	mypy src/

quality: lint format-check type-check ## Run all code quality checks

shell: ## Open Python shell with app context
	docker compose run --rm app python

clean: ## Clean up generated files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete

e2e: ## Run E2E tests (headless). Options: TAGS=@tag SCENARIO="name" FILE=features/foo.feature
	@echo "Starting E2E stack..."
	docker compose -f docker-compose.e2e.yml up -d --build --wait
	@echo "Running E2E tests..."
	E2E_APP_URL=http://localhost:8001 \
	E2E_DB_URL=postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e \
	pytest -c pytest-e2e.ini tests/e2e/ -v \
		$(if $(TAGS),-m "$(TAGS)",) \
		$(if $(SCENARIO),-k "$(SCENARIO)",) \
		$(if $(FILE),$(FILE),) \
		|| (docker compose -f docker-compose.e2e.yml down; exit 1)
	docker compose -f docker-compose.e2e.yml down

e2e-headed: ## Run E2E tests with browser visible (for debugging)
	@echo "Starting E2E stack..."
	docker compose -f docker-compose.e2e.yml up -d --build --wait
	@echo "Running E2E tests (headed)..."
	E2E_APP_URL=http://localhost:8001 \
	E2E_DB_URL=postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e \
	pytest -c pytest-e2e.ini tests/e2e/ -v --headed \
		$(if $(TAGS),-m "$(TAGS)",) \
		$(if $(SCENARIO),-k "$(SCENARIO)",) \
		$(if $(FILE),$(FILE),) \
		|| (docker compose -f docker-compose.e2e.yml down; exit 1)
	docker compose -f docker-compose.e2e.yml down

e2e-up: ## Start E2E Docker stack without running tests
	docker compose -f docker-compose.e2e.yml up -d --build --wait

e2e-down: ## Stop E2E Docker stack
	docker compose -f docker-compose.e2e.yml down

e2e-logs: ## View E2E app logs
	docker compose -f docker-compose.e2e.yml logs -f app-e2e

setup: ## Run development environment setup
	./scripts/dev_setup.sh

build: ## Build Docker images
	docker compose build

rebuild: ## Rebuild Docker images from scratch
	docker compose build --no-cache

ps: ## Show running containers
	docker compose ps

health: ## Check health of all services
	@echo "Checking service health..."
	@curl -f http://localhost:8000/health || echo "❌ App health check failed"
	@curl -f http://localhost:8000/health/ready || echo "❌ App readiness check failed"

api-docs: ## Open API documentation in browser
	@echo "Opening API docs at http://localhost:8000/docs"
	@open http://localhost:8000/docs 2>/dev/null || xdg-open http://localhost:8000/docs 2>/dev/null || echo "Please open http://localhost:8000/docs in your browser"
