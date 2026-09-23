# ==============================================================================
# Retail Agentic Copilot - Intelligent Makefile
# ==============================================================================
# Includes automation for Container Orchestration, Database Administration,
# Execution Accuracy Evaluation, Testing & Development Lifecycle.
# ==============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Environment configuration
ENV_FILE ?= .env
ifeq ($(wildcard $(ENV_FILE)),)
    $(shell cp .env.example .env 2>/dev/null || true)
endif

# Detect docker-compose command (v2 or v1)
DOCKER_COMPOSE := $(shell which docker-compose 2>/dev/null || echo "docker compose")

# Styling & Colors
BLUE   := \033[36m
GREEN  := \033[32m
YELLOW := \033[33m
RED    := \033[31m
RESET  := \033[0m
BOLD   := \033[1m

## -----------------------------------------------------------------------------
## 📌 Help & Discovery
## -----------------------------------------------------------------------------

.PHONY: help
help: ## Display this interactive help menu
	@echo -e "\n${BOLD}${BLUE}Retail Agentic Copilot — Automation CLI${RESET}\n"
	@echo -e "${YELLOW}Usage:${RESET} make ${GREEN}<target>${RESET}\n"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  ${GREEN}%-20s${RESET} %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo -e "\n${BOLD}Quick Start:${RESET} ${GREEN}make setup && make up${RESET}\n"

## -----------------------------------------------------------------------------
## 🚀 Environment & Setup
## -----------------------------------------------------------------------------

.PHONY: setup
setup: init-env init-secrets install ## Initialize environment, secrets, and install backend dependencies

.PHONY: init-env
init-env: ## Create .env from .env.example if not already present
	@if [ ! -f .env ]; then \
		echo -e "${YELLOW}Creating .env from .env.example...${RESET}"; \
		cp .env.example .env; \
		echo -e "${GREEN}✓ .env file created.${RESET}"; \
	else \
		echo -e "${BLUE}ℹ .env already exists.${RESET}"; \
	fi

.PHONY: init-secrets
init-secrets: ## Ensure Docker secret files exist from .example templates
	@echo -e "${YELLOW}Setting up secret files in secrets/...${RESET}"
	@mkdir -p secrets
	@for f in secrets/*.txt.example; do \
		target="secrets/$$(basename $$f .example)"; \
		if [ ! -f "$$target" ]; then \
			cp "$$f" "$$target"; \
			echo -e "${GREEN}✓ Created $$target${RESET}"; \
		fi; \
	done

.PHONY: install
install: ## Install backend dependencies locally
	@echo -e "${YELLOW}Installing backend dependencies...${RESET}"
	cd backend && pip install -e ".[dev]"
	@echo -e "${GREEN}✓ Backend dependencies installed.${RESET}"

## -----------------------------------------------------------------------------
## 🐳 Docker & Container Lifecycle
## -----------------------------------------------------------------------------

.PHONY: up
up: init-env init-secrets ## Launch full container stack (builds only if missing)
	@echo -e "${YELLOW}Starting container fleet (retail_copilot_db, retail_copilot_backend, retail_copilot_frontend)...${RESET}"
	$(DOCKER_COMPOSE) up -d
	@echo -e "${GREEN}✓ Containers launched.${RESET}"
	@echo -e "${BLUE}  • Frontend UI:${RESET}   http://localhost:3000"
	@echo -e "${BLUE}  • Backend Docs:${RESET}  http://localhost:8000/docs"
	@echo -e "${BLUE}  • Health Check:${RESET}  http://localhost:8000/healthz"
	@echo -e "${BLUE}  • PostgreSQL DB:${RESET} localhost:5433 (retail_copilot_db)"

.PHONY: build
build: ## Build or rebuild container images
	@echo -e "${YELLOW}Building container images...${RESET}"
	$(DOCKER_COMPOSE) build

.PHONY: up-build
up-build: init-env init-secrets ## Force rebuild images and start containers
	@echo -e "${YELLOW}Rebuilding and launching containers...${RESET}"
	$(DOCKER_COMPOSE) up -d --build
	@echo -e "${GREEN}✓ Containers rebuilt and launched.${RESET}"

.PHONY: down
down: ## Stop and remove all containers and network bridges
	@echo -e "${YELLOW}Stopping containers...${RESET}"
	$(DOCKER_COMPOSE) down
	@echo -e "${GREEN}✓ Containers stopped.${RESET}"

.PHONY: restart
restart: down up ## Restart the full container stack

.PHONY: ps status
ps status: ## List running containers and health statuses
	$(DOCKER_COMPOSE) ps

.PHONY: logs
logs: ## Tail streaming logs from all services
	$(DOCKER_COMPOSE) logs -f

.PHONY: logs-backend
logs-backend: ## Tail streaming logs from FastAPI backend
	$(DOCKER_COMPOSE) logs -f retail_copilot_backend

.PHONY: logs-db
logs-db: ## Tail streaming logs from PostgreSQL database
	$(DOCKER_COMPOSE) logs -f retail_copilot_db

.PHONY: logs-frontend
logs-frontend: ## Tail streaming logs from Next.js frontend
	$(DOCKER_COMPOSE) logs -f retail_copilot_frontend

## -----------------------------------------------------------------------------
## 🗄️ Database Administration
## -----------------------------------------------------------------------------

.PHONY: db-shell-agent
db-shell-agent: ## Open psql shell as agent_ro role (read-only semantic layer)
	@echo -e "${YELLOW}Connecting as agent_ro (read-only)...${RESET}"
	docker exec -it retail_copilot_db psql -U agent_ro -d ecommerce

.PHONY: db-shell-admin
db-shell-admin: ## Open psql shell as postgres superuser
	@echo -e "${YELLOW}Connecting as postgres superuser...${RESET}"
	docker exec -it retail_copilot_db psql -U postgres -d ecommerce

.PHONY: db-reset
db-reset: ## Hard reset database volume and re-seed 1M records (Caution: wipes data)
	@echo -e "${RED}Resetting PostgreSQL volume and restarting retail_copilot_db...${RESET}"
	$(DOCKER_COMPOSE) down -v
	$(DOCKER_COMPOSE) up -d retail_copilot_db
	@echo -e "${GREEN}✓ Fresh database re-initialized with seed dataset.${RESET}"

## -----------------------------------------------------------------------------
## 🧪 Testing & Evaluation Harness
## -----------------------------------------------------------------------------

.PHONY: test
test: ## Run unit and integration tests with pytest
	@echo -e "${YELLOW}Running backend test suite...${RESET}"
	cd backend && pytest -v

.PHONY: test-slow
test-slow: ## Run slow integration tests (including materialized view refresh)
	@echo -e "${YELLOW}Running slow integration tests...${RESET}"
	cd backend && pytest -m slow -v

.PHONY: eval
eval: ## Run Execution Accuracy (EX) benchmark with default mock router
	@echo -e "${YELLOW}Running EX Evaluation Harness (Mock Provider)...${RESET}"
	python eval/eval_harness.py --provider mock --threshold 0.9

.PHONY: eval-gemini
eval-gemini: ## Run Execution Accuracy (EX) benchmark with Gemini provider
	@echo -e "${YELLOW}Running EX Evaluation Harness (Gemini Provider)...${RESET}"
	python eval/eval_harness.py --provider gemini --threshold 0.9

.PHONY: api-health
api-health: ## Verify backend health check via HTTP request
	@echo -e "${YELLOW}Querying http://localhost:8000/healthz...${RESET}"
	@curl -sf http://localhost:8000/healthz | python -m json.tool || echo -e "${RED}Backend is unreachable.${RESET}"

## -----------------------------------------------------------------------------
## 🧹 Maintenance & Cleanup
## -----------------------------------------------------------------------------

.PHONY: clean
clean: ## Remove temporary python bytecode and test cache artifacts
	@echo -e "${YELLOW}Cleaning cache files...${RESET}"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".next" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo -e "${GREEN}✓ Caches cleaned.${RESET}"

.PHONY: clean-all
clean-all: clean ## Complete teardown: remove containers, volumes, networks, and caches
	@echo -e "${RED}Teardown of all containers and volumes...${RESET}"
	$(DOCKER_COMPOSE) down -v --remove-orphans
	@echo -e "${GREEN}✓ Complete cleanup finished.${RESET}"
