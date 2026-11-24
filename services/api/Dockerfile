FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr tesseract-ocr-hun tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/api/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY services/common/teacher_common /app/teacher_common
COPY config /app/config
COPY services/api/app /app

ARG EMBEDDING_MODEL=intfloat/multilingual-e5-base
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

ENV HF_HUB_OFFLINE=1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

