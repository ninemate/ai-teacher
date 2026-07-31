FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/voice/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY services/voice/app /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
