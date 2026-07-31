from collections.abc import Iterable
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from teacher_common.config import get_settings

settings = get_settings()


@lru_cache(maxsize=1)
def get_embedding_model() -> SentenceTransformer:
    return SentenceTransformer(settings.embedding_model, device=settings.embedding_device)


def embed_passages(texts: Iterable[str]) -> list[list[float]]:
    model = get_embedding_model()
    inputs = [f"passage: {text}" for text in texts]
    return model.encode(inputs, normalize_embeddings=True).tolist()


def embed_query(text: str) -> list[float]:
    model = get_embedding_model()
    result = model.encode([f"query: {text}"], normalize_embeddings=True)
    return result[0].tolist()


def embedding_dimension() -> int:
    model = get_embedding_model()
    return int(model.get_sentence_embedding_dimension())


def warmup_embeddings() -> None:
    model = get_embedding_model()
    model.encode(["warmup"], normalize_embeddings=True)

