.PHONY: install dev dev-build dev-rebuild dev-backend dev-frontend demo-inventory demo-start demo-stop demo-reset demo-teardown demo-cleanup-image demo-cleanup-build cleanup-build demo-preflight e2e-discovery docs-validate phase2-package-boundary phase6-archive-verify public-mirror-verify test test-integration test-e2e test-faults lint typecheck security test-mcp-hermetic test-mcp-postgres test-mcp eval-validate load-test compose-verify db-up db-migrate db-seed db-down knowledge-ingest

DEMO_PROJECT ?= supportguard-v15ui
PHASE2_CANDIDATE_SHA ?= $(shell git rev-parse --verify HEAD)
PHASE2_PACKAGE_BOUNDARY_RECEIPT ?= dist/phase2/package-boundary-$(PHASE2_CANDIDATE_SHA).json
PHASE6_ARCHIVE_TRANSITION_MANIFEST ?= validation/evidence/interview_v2/phase6/archive-transition-manifest.v1.json

TEST_POSTGRES_PORT ?= 5432
TEST_REDIS_PORT ?= 6379
TEST_DATABASE_URL ?= postgresql+asyncpg://supportguard:supportguard@localhost:$(TEST_POSTGRES_PORT)/supportguard
TEST_WORKER_DATABASE_URL ?= postgresql+asyncpg://supportguard_worker:supportguard_worker@localhost:$(TEST_POSTGRES_PORT)/supportguard
TEST_MCP_READ_DATABASE_URL ?= postgresql+asyncpg://supportguard_read_mcp:supportguard_read_mcp@localhost:$(TEST_POSTGRES_PORT)/supportguard
TEST_MCP_ACTION_DATABASE_URL ?= postgresql+asyncpg://supportguard_action_mcp:supportguard_action_mcp@localhost:$(TEST_POSTGRES_PORT)/supportguard
TEST_REDIS_URL ?= redis://integration:integration_dev@localhost:$(TEST_REDIS_PORT)/0
TEST_WORKER_REDIS_URL ?= redis://worker:worker_dev@localhost:$(TEST_REDIS_PORT)/0
TEST_RECONCILER_REDIS_URL ?= redis://reconciler:reconciler_dev@localhost:$(TEST_REDIS_PORT)/0
TEST_API_REDIS_URL ?= redis://api:api_dev@localhost:$(TEST_REDIS_PORT)/0

install:
	uv sync --all-packages --dev --extra local-embeddings
	pnpm --dir frontend install --frozen-lockfile

dev:
	uv run python scripts/dev_up.py

dev-build:
	uv run python scripts/dev_up.py --build-only

dev-rebuild:
	uv run python scripts/dev_up.py --rebuild

dev-backend:
	APP_RELOAD=true uv run supportguard serve

dev-frontend:
	pnpm --dir frontend dev

demo-inventory:
	uv run python scripts/demo_environment.py inventory

demo-start:
	uv run python scripts/demo_environment.py start --project $(DEMO_PROJECT) $(if $(BUILD),--build,)

demo-stop:
	uv run python scripts/demo_environment.py stop --project $(DEMO_PROJECT)

demo-reset:
	uv run python scripts/demo_environment.py reset --project $(DEMO_PROJECT) --confirm-project $(CONFIRM_PROJECT) $(if $(BUILD),--build,)

demo-teardown:
	uv run python scripts/demo_environment.py teardown --project $(DEMO_PROJECT) $(if $(DELETE_VOLUMES),--delete-volumes --confirm-project $(CONFIRM_PROJECT),)

demo-cleanup-image:
	uv run python scripts/demo_environment.py cleanup-image --image $(IMAGE) --confirm-image $(CONFIRM_IMAGE)

demo-cleanup-build:
	uv run python scripts/demo_environment.py cleanup-build --project $(DEMO_PROJECT) --confirm-project $(CONFIRM_PROJECT)

cleanup-build: demo-cleanup-build

demo-preflight:
	docker compose -p $(DEMO_PROJECT) run --rm --no-deps bootstrap-demo supportguard demo temporal-refresh --tenant tenant_demo
	docker compose -p $(DEMO_PROJECT) run --rm --no-deps bootstrap-demo supportguard demo temporal-preflight --tenant tenant_demo
	docker compose -p $(DEMO_PROJECT) run --rm --no-deps worker supportguard demo resource-preflight --tenant tenant_demo

e2e-discovery:
	uv run python scripts/validate_e2e_discovery.py

docs-validate:
	uv run python scripts/validate_interview_docs.py

phase2-package-boundary:
	uv run --frozen python scripts/run_phase2_package_boundary.py \
		--mode candidate \
		--expected-head "$(PHASE2_CANDIDATE_SHA)" \
		--output "$(PHASE2_PACKAGE_BOUNDARY_RECEIPT)"

phase6-archive-verify:
	uv run python scripts/phase6_archive_transition.py verify \
		--output "$(PHASE6_ARCHIVE_TRANSITION_MANIFEST)" \
		--require-absent

public-mirror-verify:
	uv run pytest -q \
		backend/tests/test_public_mirror_contract.py \
		backend/tests/test_phase6_archive_transition.py \
		backend/tests/test_interview_v2_phase0_contracts.py \
		backend/tests/test_interview_v2_test_disposition.py

test:
	uv run pytest -m "not mcp"
	pnpm --dir frontend test

test-integration:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_FINALIZER_DATABASE_URL=$(TEST_DATABASE_URL) TEST_WORKER_DATABASE_URL=$(TEST_WORKER_DATABASE_URL) TEST_POSTGRES_PORT=$(TEST_POSTGRES_PORT) TEST_REDIS_URL=$(TEST_REDIS_URL) TEST_WORKER_REDIS_URL=$(TEST_WORKER_REDIS_URL) TEST_RECONCILER_REDIS_URL=$(TEST_RECONCILER_REDIS_URL) TEST_API_REDIS_URL=$(TEST_API_REDIS_URL) uv run python scripts/run_isolated_integration.py integration

test-e2e:
	pnpm --dir frontend test:e2e

test-faults:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) TEST_WORKER_REDIS_URL=$(TEST_WORKER_REDIS_URL) uv run pytest backend/tests/test_runtime_queue.py backend/tests/test_segments.py backend/tests/test_attempt_ledger.py
	uv run python scripts/process_faults.py

lint:
	uv run ruff check backend validation/src scripts
	pnpm --dir frontend lint

typecheck:
	uv run mypy backend/src
	MYPYPATH=backend/src:validation/src uv run mypy --namespace-packages --explicit-package-bases validation/src
	pnpm --dir frontend build

security:
	uv run bandit -c pyproject.toml -r backend/src validation/src
	uv run python scripts/pip_audit_retry.py
	uv run python scripts/security_boundaries.py

test-mcp-hermetic:
	uv run python scripts/run_mcp_test_partitions.py hermetic

test-mcp-postgres:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) MCP_READ_DATABASE_URL=$(TEST_MCP_READ_DATABASE_URL) MCP_ACTION_DATABASE_URL=$(TEST_MCP_ACTION_DATABASE_URL) uv run python scripts/run_mcp_test_partitions.py postgres

test-mcp:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) MCP_READ_DATABASE_URL=$(TEST_MCP_READ_DATABASE_URL) MCP_ACTION_DATABASE_URL=$(TEST_MCP_ACTION_DATABASE_URL) uv run python scripts/run_mcp_test_partitions.py all

eval-validate:
	uv run --package supportguard-validation supportguard-validation eval validate

load-test:
	uv run python scripts/load_test.py

compose-verify:
	uv run python scripts/compose_verify.py

db-up:
	docker compose up -d postgres

db-migrate:
	uv run supportguard db baseline-upgrade

db-seed:
	uv run supportguard db seed

knowledge-ingest:
	uv run supportguard knowledge ingest

db-down:
	docker compose down
