from typing import Iterable, List, Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from teacher_common.config import get_settings
from teacher_common.embeddings import embedding_dimension


settings = get_settings()


def get_client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url)


def ensure_collection() -> None:
    client = get_client()
    collections = {item.name for item in client.get_collections().collections}
    if settings.qdrant_collection not in collections:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=embedding_dimension(),
                distance=Distance.COSINE,
            ),
        )


def recreate_collection() -> None:
    client = get_client()
    client.recreate_collection(
        collection_name=settings.qdrant_collection,
        vectors_config=VectorParams(
            size=embedding_dimension(),
            distance=Distance.COSINE,
        ),
    )


def upsert_points(points: Sequence[PointStruct]) -> None:
    client = get_client()
    client.upsert(collection_name=settings.qdrant_collection, points=points)


def delete_points_for_source(source_path: str) -> None:
    client = get_client()
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[
                    FieldCondition(
                        key="source_path",
                        match=MatchValue(value=source_path),
                    )
                ]
            )
        ),
    )


def search(query_vector: List[float], limit: int):
    client = get_client()
    return client.search(
        collection_name=settings.qdrant_collection,
        query_vector=query_vector,
        limit=limit,
        with_payload=True,
    )


def build_points(
    source_path: str,
    title: str,
    author: str | None,
    language: str | None,
    chunk_rows: Iterable[dict],
) -> List[PointStruct]:
    points: List[PointStruct] = []
    for row in chunk_rows:
        points.append(
            PointStruct(
                id=row["point_id"],
                vector=row["vector"],
                payload={
                    "chunk_id": row["chunk_id"],
                    "source_path": source_path,
                    "title": title,
                    "author": author,
                    "language": language,
                    "locator": row["locator"],
                    "position": row["position"],
                    "text": row["text"],
                    "preview": row["preview"],
                },
            )
        )
    return points
