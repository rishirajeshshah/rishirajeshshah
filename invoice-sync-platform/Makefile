.PHONY: help up down logs migrate shell test lint install

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Docker Compose ────────────────────────────────────────────────────────────

up:  ## Start all services (API + Worker + Postgres + Redis)
	@cp -n .env.example .env 2>/dev/null || true
	docker compose up --build -d
	@echo ""
	@echo "  API:    http://localhost:8000"
	@echo "  Docs:   http://localhost:8000/docs"
	@echo "  Health: http://localhost:8000/health"
	@echo ""
	@echo "  See logs: make logs"

down:  ## Stop all services
	docker compose down

down-v:  ## Stop all services and remove volumes (fresh start)
	docker compose down -v

logs:  ## Follow all service logs
	docker compose logs -f

logs-api:  ## Follow API logs only
	docker compose logs -f api

logs-worker:  ## Follow worker logs only
	docker compose logs -f worker

monitoring:  ## Start with Flower (Celery monitoring at http://localhost:5555)
	docker compose --profile monitoring up -d

# ── Database ──────────────────────────────────────────────────────────────────

migrate:  ## Run Alembic migrations inside Docker
	docker compose run --rm migrate

migrate-local:  ## Run Alembic migrations locally (needs local Postgres running)
	alembic upgrade head

migration-new:  ## Create a new migration (usage: make migration-new MSG="description")
	alembic revision --autogenerate -m "$(MSG)"

db-shell:  ## Open psql shell
	docker compose exec postgres psql -U isp -d isp

# ── Development ───────────────────────────────────────────────────────────────

install:  ## Install dependencies locally
	pip install -e ".[dev]"

shell:  ## Open a Python shell with app context
	docker compose exec api python -c "from src.config import settings; import IPython; IPython.embed()"

test:  ## Run tests
	docker compose exec api pytest tests/ -v

lint:  ## Run ruff linter
	ruff check src/

# ── Quick API Test ────────────────────────────────────────────────────────────

demo-create-tenant:  ## Create a demo tenant
	curl -s -X POST http://localhost:8000/api/v1/tenants \
	  -H "Content-Type: application/json" \
	  -H "X-API-Key: dev-api-key-change-in-prod" \
	  -d '{"name": "Demo Corp", "erp_source": "netsuite", "processor_target": "paypal"}' \
	  | python3 -m json.tool

demo-sync:  ## Trigger a demo sync (replace TENANT_ID with output from demo-create-tenant)
	@echo "Usage: TENANT_ID=<id> make demo-sync"
	@[ -n "$(TENANT_ID)" ] || (echo "Set TENANT_ID first"; exit 1)
	curl -s -X POST http://localhost:8000/api/v1/sync-jobs \
	  -H "Content-Type: application/json" \
	  -H "X-API-Key: dev-api-key-change-in-prod" \
	  -d '{"tenant_id": "$(TENANT_ID)", "erp_source": "netsuite", "processor_target": "paypal", "mode": "manual"}' \
	  | python3 -m json.tool

list-adapters:  ## List all registered adapters
	curl -s http://localhost:8000/api/v1/adapters \
	  -H "X-API-Key: dev-api-key-change-in-prod" \
	  | python3 -m json.tool
