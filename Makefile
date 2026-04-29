COMPOSE_BASE=docker compose --env-file .env -f compose/docker-compose.yml
COMPOSE_DEV=$(COMPOSE_BASE) -f compose/docker-compose.dev.yml

.PHONY: dev-up dev-down dev-logs deploy-dev deploy-prod healthcheck reindex compose-config nvidia-check test-chat test-ingest

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

test-chat:
	curl -fsS -X POST "http://$${TEACHER_HTTP_BIND:-127.0.0.1}:$${TEACHER_HTTP_PORT:-8080}/chat" \
		-H "Content-Type: application/json" \
		-d '{"question":"Foglald össze röviden, hogyan működik a RAG.", "level":"beginner"}'

test-ingest:
	curl -fsS "http://$${TEACHER_HTTP_BIND:-127.0.0.1}:$${TEACHER_INGEST_PORT:-8081}/ingest/status"

