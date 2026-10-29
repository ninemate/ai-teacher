COMPOSE_BASE=docker compose --env-file .env -f compose/docker-compose.yml
COMPOSE_DEV=$(COMPOSE_BASE) -f compose/docker-compose.dev.yml
COMPOSE_VOICE=$(COMPOSE_DEV) -f compose/docker-compose.voice.example.yml

.PHONY: dev-up dev-up-voice dev-down dev-logs deploy-dev deploy-prod healthcheck reindex compose-config nvidia-check test test-chat test-ingest hash-password

dev-up:
	$(COMPOSE_DEV) up -d --build

# Dev stack plus the offline Hungarian voice service (Piper TTS, Whisper STT on the CPU).
dev-up-voice:
	$(COMPOSE_VOICE) up -d --build

dev-down:
	$(COMPOSE_VOICE) down

dev-logs:
	$(COMPOSE_DEV) logs -f --tail=200

deploy-dev:
	ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/deploy.yml

deploy-prod:
	ansible-playbook -i ansible/inventory/prod.example.ini ansible/playbooks/deploy.yml

healthcheck:
	ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/healthcheck.yml

reindex:
	ansible-playbook -i ansible/inventory/dev.ini ansible/playbooks/reindex.yml

compose-config:
	$(COMPOSE_DEV) config

nvidia-check:
	nvidia-smi
	docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

test:
	python -m pytest

# Needs TEACHER_USER and TEACHER_PASSWORD in the environment.
test-chat:
	scripts/smoke-chat.sh "http://$${TEACHER_HTTP_BIND:-127.0.0.1}:$${TEACHER_HTTP_PORT:-8080}"

test-ingest:
	curl -fsS "http://127.0.0.1:$${TEACHER_INGEST_PORT:-8081}/ingest/status" \
		-H "Authorization: Bearer $$(sed -n 's/^TEACHER_INGEST_TOKEN=//p' .env)"

# Prints an argon2 hash for TEACHER_AUTH_USERS.
hash-password:
	$(COMPOSE_BASE) run --rm --no-deps api python auth.py

