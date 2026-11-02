from collections.abc import Iterable, Sequence
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
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
        try:
            client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config=VectorParams(
                    size=embedding_dimension(),
                    distance=Distance.COSINE,
                ),
            )
        except UnexpectedResponse as exc:
            # The api and ingestion services start together on an empty store; the other one was first.
            if exc.status_code != 409:
                raise


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


def delete_points_for_source(source_path: str, workspace_id: str | None = None) -> None:
    client = get_client()
    conditions = [FieldCondition(key="source_path", match=MatchValue(value=source_path))]
    if workspace_id is not None:
        workspace_match = FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id))
        if workspace_id == "legacy":
            # The migration assigns old rows to legacy; their old vector payloads have no workspace_id field.
            query_filter = Filter(
                must=conditions,
                should=[workspace_match, IsEmptyCondition(is_empty=PayloadField(key="workspace_id"))],
            )
        else:
            conditions.append(workspace_match)
            query_filter = Filter(must=conditions)
    else:
        query_filter = Filter(must=conditions)
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(
            filter=query_filter
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


def scroll_document(source_path: str) -> list[dict]:
    """Every stored chunk payload of one document, in no particular order (callers sort by ``position``)."""
    client = get_client()
    flt = Filter(must=[FieldCondition(key="source_path", match=MatchValue(value=source_path))])
    payloads: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=flt,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend(point.payload or {} for point in points)
        if offset is None:
            return payloads


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
    file_id: str | None = None,
) -> list[PointStruct]:
    points: list[PointStruct] = []
    for row in chunk_rows:
        points.append(
            PointStruct(
                id=row["point_id"],
                vector=row["vector"],
                payload={
                    "chunk_id": row["chunk_id"],
                    # Who lent the words: what file, where in it, and the answer's confidence in the text.
                    "file_id": file_id or row["file_id"],
                    "file_name": Path(source_path).name,
                    "source_path": source_path,
                    "workspace_id": workspace_id,
                    "title": title,
                    "author": author,
                    "language": language,
                    "locator": row["locator"],
                    "page": row.get("page"),
                    "chapter": row.get("chapter"),
                    "ocr": row.get("ocr", False),
                    "ocr_confidence": row.get("ocr_confidence"),
                    "position": row["position"],
                    "text": row["text"],
                    "preview": row["preview"],
                },
            )
        )
    return points
