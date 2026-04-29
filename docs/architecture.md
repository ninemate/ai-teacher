# Architecture

The MVP is a text-first RAG system designed for a dedicated teacher-agent VM. It is intentionally separate from any other AI workload stack.

## Core Services

- `api`: mobile-friendly web UI and JSON API
- `ingestion`: scans the mounted share and updates local indexes
- `qdrant`: vector storage
- `ollama`: local LLM runtime
- `sqlite`: local metadata state stored on the host, not a separate container
- `voice`: optional future service for STT/TTS

## High-Level Flow

```mermaid
flowchart TD
  Phone[Phone Browser] --> UI[FastAPI Web UI]
  UI --> API[Chat and Teaching API]
  API --> RETRIEVE[Retrieve Top-K Chunks]
  RETRIEVE --> Q[(Qdrant)]
  API --> LLM[Ollama]
  Share[Mounted Share] --> ING[Ingestion Service]
  ING --> PARSE[Parse and Chunk]
  PARSE --> EMBED[Embedding Model]
  EMBED --> Q
  ING --> S[(SQLite Metadata)]
  API --> S
```

## Ingestion Flow

```mermaid
flowchart LR
  A[Mounted Share] --> B[Scan Files]
  B --> C{Changed?}
  C -->|No| D[Skip]
  C -->|Yes| E[Extract Text]
  E --> F[Chunk]
  F --> G[Embed]
  G --> H[(Qdrant)]
  E --> I[(SQLite Metadata)]
```

## Chat Flow

```mermaid
flowchart LR
  Q1[Hungarian Question] --> Q2[Embed Query]
  Q2 --> Q3[(Qdrant Search)]
  Q3 --> Q4[Context Assembly]
  Q4 --> Q5[Teacher Prompt in Hungarian]
  Q5 --> Q6[Ollama Model]
  Q6 --> Q7[Hungarian Answer with Sources]
```

## Mobile Access Flow

```mermaid
flowchart TD
  Phone[Father's Phone] -->|Preferred: VPN| VPN[WireGuard or Tailscale]
  VPN --> Host[Teacher-Agent VM]
  Host --> UI[FastAPI UI]
  Phone -->|Alternative: HTTPS + auth| Caddy[Caddy]
  Caddy --> UI
```

## Optional Voice Flow

```mermaid
flowchart LR
  Mic[Phone Microphone] --> STT[Optional STT Service]
  STT --> API[Teacher API]
  API --> TTS[Optional TTS Service]
  TTS --> Audio[Hungarian Audio Reply]
```

## VM Split Example

```mermaid
flowchart TB
  Host[Physical Host: up to 8x RTX 3070] --> VM1[Image Generation VM: 7 GPUs]
  Host --> VM2[Teacher-Agent VM: 1 GPU]
  VM2 --> Teacher[Teacher-Agent Stack]
```

## Design Notes

- The source library remains on the share.
- Index data lives locally for speed.
- Chunk text is stored locally as part of the RAG index payload.
- Sequential ingestion is a deliberate default to protect `16 GB RAM`.

