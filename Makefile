.PHONY: help build up down clean logs shell redis test-all test-prefork test-gevent test-eventlet test-threads demo-crash demo-gdb demo-strace

# Docker Compose commands
COMPOSE := docker compose

help:
	@echo "Celery Fork-Safety Testing - Make Commands"
	@echo ""
	@echo "Build & Setup:"
	@echo "  make build          Build Docker image"
	@echo "  make redis          Start Redis in background"
	@echo "  make clean          Clean output directories"
	@echo ""
	@echo "Celery Tests:"
	@echo "  make test-all       Run all pool type tests"
	@echo "  make test-prefork   Test prefork pool (expects failure)"
	@echo "  make test-gevent    Test gevent pool (expects success)"
	@echo "  make test-eventlet  Test eventlet pool (expects success)"
	@echo "  make test-threads   Test threads pool (expects success)"
	@echo ""
	@echo "Demo Crash Tests:"
	@echo "  make demo-crash     Run demo crash (normal)"
	@echo "  make demo-gdb       Run demo crash with GDB debugging"
	@echo "  make demo-strace    Run demo crash with strace"
	@echo ""
	@echo "Utilities:"
	@echo "  make shell          Interactive shell with Redis"
	@echo "  make logs           View logs"
	@echo "  make down           Stop all services"
	@echo ""
	@echo "Quick Start:"
	@echo "  make build && make redis && make test-all"

build:
	$(COMPOSE) build

redis:
	$(COMPOSE) up -d redis

up:
	$(COMPOSE) up

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) run --rm clean

logs:
	$(COMPOSE) logs -f

# Celery test commands
test-all:
	$(COMPOSE) run --rm celery-test-all

test-prefork:
	$(COMPOSE) run --rm celery-test-prefork

test-gevent:
	$(COMPOSE) run --rm celery-test-gevent

test-eventlet:
	$(COMPOSE) run --rm celery-test-eventlet

test-threads:
	$(COMPOSE) run --rm celery-test-threads

# Demo crash commands
demo-crash:
	$(COMPOSE) run --rm demo-crash

demo-gdb:
	$(COMPOSE) run --rm demo-gdb

demo-strace:
	$(COMPOSE) run --rm demo-strace

# Utility commands
shell:
	$(COMPOSE) run --rm shell

