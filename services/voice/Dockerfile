FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/models/hf

WORKDIR /app

COPY services/voice/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Only for running Whisper on the GPU (compose/docker-compose.voice.nvidia.example.yml): CTranslate2 needs
# cuBLAS and cuDNN 9 at runtime. Adds about 1.5 GB to the image, so it is off by default.
ARG VOICE_CUDA=false
RUN if [ "$VOICE_CUDA" = "true" ]; then pip install "nvidia-cublas-cu12==12.*" "nvidia-cudnn-cu12==9.*"; fi
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

# Models are baked into the image so the running service never reaches the network.
ARG VOICE_STT_MODEL=large-v3-turbo
ARG VOICE_TTS_VOICES=hu_HU-imre-medium,hu_HU-anna-medium,hu_HU-berta-medium
RUN python -c "from faster_whisper.utils import download_model; download_model('${VOICE_STT_MODEL}')" \
    && python -m piper.download_voices --data-dir /models/piper $(echo "${VOICE_TTS_VOICES}" | tr ',' ' ')

ENV HF_HUB_OFFLINE=1 \
    VOICE_TTS_VOICE_DIR=/models/piper

COPY services/voice/app /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8090"]
