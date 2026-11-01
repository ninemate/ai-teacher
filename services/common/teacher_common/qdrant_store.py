from collections.abc import Iterable, Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
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
    # documents.source_path is unique; matching it also removes legacy points that predate workspace payloads.
    client = get_client()
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="source_path", match=MatchValue(value=source_path))]
            )
        ),
    )


def delete_points_for_workspace(workspace_id: str) -> None:
    client = get_client()
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))])
        ),
    )


def search(
    query_vector: list[float],
    limit: int,
    workspace_ids: set[str],
    source_paths: list[str] | None = None,
):
    if not workspace_ids:
        return []
    client = get_client()
    conditions = [FieldCondition(key="workspace_id", match=MatchAny(any=sorted(workspace_ids)))]
    if source_paths:
        conditions.append(FieldCondition(key="source_path", match=MatchAny(any=source_paths)))
    return client.search(
        collection_name=settings.qdrant_collection,
        query_vector=query_vector,
        limit=limit,
        query_filter=Filter(must=conditions),
        with_payload=True,
    )


def build_points(
    source_path: str,
    workspace_id: str,
    title: str,
    author: str | None,
    language: str | None,
    chunk_rows: Iterable[dict],
) -> list[PointStruct]:
    points: list[PointStruct] = []
    for row in chunk_rows:
        points.append(
            PointStruct(
                id=row["point_id"],
                vector=row["vector"],
                payload={
                    "chunk_id": row["chunk_id"],
                    "source_path": source_path,
                    "workspace_id": workspace_id,
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
