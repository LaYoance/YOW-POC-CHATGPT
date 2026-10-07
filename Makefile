.PHONY: help dev start stop restart status test

PORT ?= 8000

help:
	@echo "Commandes pour ChatGPT Integration POC :"
	@echo "  make dev / make start - Lance le serveur FastAPI (port $(PORT))"
	@echo "  make stop             - Arrête le serveur (port $(PORT))"
	@echo "  make restart          - Redémarre le serveur (port $(PORT))"
	@echo "  make status           - Vérifie si le serveur est actif"
	@echo "  make test             - Exécute les tests pytest"

dev: start

start:
	DEBUG=false ./run.sh

stop:
	@echo "Arrêt du serveur chat sur le port $(PORT)..."
	@fuser -k -TERM $(PORT)/tcp 2>/dev/null || pkill -f "[u]vicorn.*app.main:app" 2>/dev/null || true
	@echo "Serveur chat arrêté."

restart: stop dev

status:
	@if fuser $(PORT)/tcp >/dev/null 2>&1; then \
		echo "Serveur chat ACTIF sur le port $(PORT) :"; \
		ss -tulpn | grep $(PORT); \
	else \
		echo "Serveur chat INACTIF (port $(PORT) libre)"; \
	fi

test:
	./venv/bin/pytest tests/
