# Thin aliases. Logic lives in scripts/ (documented in docs/commands.md); run `make help`.
UV := uv run --frozen --extra dev
E2E_ENV := E2E_APP_URL=http://localhost:8001 \
	E2E_DB_URL=postgresql://postgres:postgres@localhost:5435/submissions_checker_e2e
SHELLCHECK := docker run --rm -v "$(CURDIR):/mnt" -w /mnt koalaman/shellcheck:stable

.PHONY: help install setup vendor-assets up down logs logs-app db-shell health api-docs \
	test test-unit test-integration test-functional test-ops test-backup \
	lint lint-fix format format-check type-check shellcheck quality clean \
	e2e e2e-up e2e-down e2e-logs \
	observability-up observability-down alloy-logs dashboards-json dashboards alerting \
	prod-db prod-backup prod-backup-status

help: ## List targets (details: docs/commands.md)
	@awk 'BEGIN {FS = ":.*?## "} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Dev
install: ## Install locked Python deps into .venv (after pulling a lockfile change)
	uv sync --frozen --extra dev

setup: vendor-assets ## First-time setup: install deps, create .env, start postgres
	./dev_setup.sh

vendor-assets: ## Fetch proctoring models into static/vendor/ (idempotent; up runs it)
	python3 scripts/fetch_vendor_assets.py

up: vendor-assets ## Start dev stack: postgres, minio, app on :8000 (hot reload)
	docker compose up -d

down: ## Stop dev stack (data volumes kept)
	docker compose down

logs: ## Follow all dev logs
	docker compose logs -f

logs-app: ## Follow app log only
	docker compose logs -f app

db-shell: ## psql into the dev database
	docker compose exec postgres psql -U postgres -d submissions_checker

health: ## Probe /health and /health/ready of the dev app
	@curl -fsS http://localhost:8000/health && echo
	@curl -fsS http://localhost:8000/health/ready && echo

api-docs: ## Open Swagger UI of the dev app
	@xdg-open http://localhost:8000/docs 2>/dev/null || open http://localhost:8000/docs 2>/dev/null || echo http://localhost:8000/docs

##@ Test
test: ## Whole Python suite with coverage (~6 min, needs Docker)
	$(UV) pytest --cov=submissions_checker --cov-report=term-missing --cov-report=html

test-unit: ## Unit tests only (seconds, no Docker)
	$(UV) pytest tests/unit -q

test-integration: ## Integration tests (testcontainers Postgres)
	$(UV) pytest tests/integration -q

test-functional: ## Functional API tests (real app over ASGI)
	$(UV) pytest tests/functional -q

test-ops: ## Offline checks of scripts/ops (no network)
	bash tests/ops/test_ops_scripts.sh

test-backup: ## Backup -> destroy -> restore roundtrip in an isolated compose project (~1 min)
	bash tests/ops/backup_roundtrip.sh

##@ Quality
lint: ## Ruff lint
	$(UV) ruff check src tests

lint-fix: ## Ruff lint with autofix
	$(UV) ruff check --fix src tests

format: ## Ruff format (writes)
	$(UV) ruff format src tests

format-check: ## Ruff format check (CI)
	$(UV) ruff format --check src tests

type-check: ## mypy on src
	$(UV) mypy src

shellcheck: ## shellcheck all shell scripts (via Docker)
	$(SHELLCHECK) docker/backup/backup.sh docker/minio/init.sh scripts/ops/*.sh tests/ops/*.sh dev_setup.sh

quality: lint format-check type-check shellcheck ## Everything CI checks except tests

clean: ## Remove caches and coverage output
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name htmlcov \) -prune -exec rm -rf {} +
	rm -f .coverage coverage.xml

##@ E2E
e2e: vendor-assets ## Browser tests. TAGS=@tag SCENARIO="name" FILE=path HEADED=1
	docker compose -f docker-compose.e2e.yml up -d --build --wait
	$(E2E_ENV) uv run --frozen --extra e2e pytest -c pytest-e2e.ini tests/e2e/ -v \
		$(if $(HEADED),--headed,) $(if $(TAGS),-m "$(TAGS)",) \
		$(if $(SCENARIO),-k "$(SCENARIO)",) $(if $(FILE),$(FILE),) \
		|| (docker compose -f docker-compose.e2e.yml down; exit 1)
	docker compose -f docker-compose.e2e.yml down

e2e-up: ## Start the e2e stack only (to debug against :8001)
	docker compose -f docker-compose.e2e.yml up -d --build --wait

e2e-down: ## Stop the e2e stack
	docker compose -f docker-compose.e2e.yml down

e2e-logs: ## Follow e2e app log
	docker compose -f docker-compose.e2e.yml logs -f app-e2e

##@ Observability (docs/observability.md)
observability-up: ## Local Prometheus+Loki+Grafana+Alloy; Grafana at :3000
	# Compose's resolved project name (not just this dir's basename, e.g. in a worktree)
	# has to be passed in explicitly, or Alloy's Docker discovery filters on the wrong
	# label and silently tails nothing.
	SUBCHK_COMPOSE_PROJECT="$$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')" \
		docker compose --profile observability up -d prometheus loki grafana alloy

observability-down: ## Stop the local observability harness
	docker compose --profile observability rm -sf prometheus loki grafana alloy

alloy-logs: ## Follow Alloy (rejected pushes show here)
	docker compose --profile observability logs -f alloy

dashboards-json: ## Regenerate dashboard/alert JSON (never hand-edit them)
	python3 observability/grafana/build_dashboards.py
	python3 observability/grafana/build_alerting.py

dashboards: ## Push dashboards to Grafana Cloud (GRAFANA_* in .env)
	@set -a; . ./.env; set +a; python3 observability/grafana/push.py dashboards

alerting: ## Push contact point + alert rules to Grafana Cloud
	@set -a; . ./.env; set +a; python3 observability/grafana/push.py alerting

##@ Prod ops (need ssh/prod.env; restore has no target on purpose)
prod-db: ## Read-only psql on prod (scripts/ops/connect-to-prod-db.sh)
	scripts/ops/connect-to-prod-db.sh

prod-backup: ## Take a prod backup now (before risky deploys)
	scripts/ops/run-prod-backup.sh --now

prod-backup-status: ## Age of the last good prod backup
	scripts/ops/run-prod-backup.sh --status
