# RAG Design

This project uses a straightforward retrieval-augmented generation flow rather than an agentic orchestration platform.

## Retrieval Steps

1. Embed the user question with a multilingual embedding model.
2. Search Qdrant for top matching chunks.
3. Assemble a bounded context window.
4. Add the Hungarian teacher prompt.
5. Ask the local model to answer only from retrieved context.

## Teacher Behavior

The default prompt instructs the model to:

- answer in Hungarian
- explain in a teacher-like style
- adapt to requested level
- admit when the library does not contain enough information
- mention sources when available
- suggest quiz or flashcard style follow-ups

## Why Qdrant + SQLite

- Qdrant is simple, maintained, and purpose-built for vectors.
- SQLite keeps metadata local without another service.
- This is smaller and easier to migrate than a full Postgres stack for the MVP.

