COMPOSE_BASE=docker compose --env-file .env -f compose/docker-compose.yml
COMPOSE_DEV=$(COMPOSE_BASE) -f compose/docker-compose.dev.yml -f compose/docker-compose.proxy.yml

.PHONY: dev-up dev-down dev-logs deploy-dev deploy-prod healthcheck reindex compose-config nvidia-check test test-chat test-ingest hash-password model-update test-egress

dev-up:
	$(COMPOSE_DEV) up -d --build

dev-down:
	$(COMPOSE_DEV) down

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

# Needs TEACHER_USER and TEACHER_PASSWORD in the environment. Speaks to the HTTPS proxy;
# the private services publish no host ports (CURL_INSECURE=1 trusts the internal CA).
test-chat:
	CURL_INSECURE=$${CURL_INSECURE:-1} scripts/smoke-chat.sh \
		"https://$${TEACHER_PROXY_HOST:-localhost}:$${TEACHER_HTTPS_PORT:-8443}"

# Asks the ingestion service for /ingest/status from inside the private network
# (the service publishes no host port). Needs `.env` on this host.
test-ingest:
	$(COMPOSE_BASE) exec -T ingestion \
		python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8081/ingest/status', headers={'Authorization': 'Bearer ' + os.environ['TEACHER_INGEST_TOKEN']}); print(urllib.request.urlopen(r).read().decode())"

# Pulls models for the internal Ollama container through a temporary edge-network
# connection, then disconnects it again. This is the only channel that may reach the
# internet; the private services keep none at runtime (docs/security.md).
model-update:
	bash scripts/model-update.sh

# Proves the egress rule (Private AI acceptance test 11). --config-only works
# without a running stack.
test-egress:
	bash scripts/test-egress-block.sh

# Prints an argon2 hash for TEACHER_AUTH_USERS.
hash-password:
	$(COMPOSE_BASE) run --rm --no-deps api python auth.py

