.PHONY: help up down test smoke verify

help:  ## show the available targets
	@echo "ApprovalFlow — make targets:"
	@echo "  make up      — build and start the whole system (docker compose up --build -d)"
	@echo "  make down    — stop and remove everything, incl. volumes"
	@echo "  make test    — run the unit + integration test suite (pytest)"
	@echo "  make verify  — D5: one-command verification (4 journeys + anti-cheese), PASS/FAIL"
	@echo "  make smoke   — the developer end-to-end run (also characterizes live Dapr stores)"

up:  ## build and start the stack
	docker compose up --build -d

down:  ## stop and remove everything (incl. volumes)
	docker compose down -v

test:  ## run the pytest suite
	pytest -q

verify:  ## D5 — the graded one-command verification
	bash scripts/verify.sh

smoke:  ## developer end-to-end run
	bash scripts/smoke-compose.sh
