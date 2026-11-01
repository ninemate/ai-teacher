"""Workspace ownership and sharing rules for private library data."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from teacher_common.models import Workspace, WorkspaceShare


def ensure_personal_workspace(session: Session, username: str) -> Workspace:
    workspace_id = f"user:{username}"
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        workspace = Workspace(id=workspace_id, name=f"{username} tudástára", owner_username=username)
        session.add(workspace)
        session.flush()
    return workspace


def accessible_workspace_ids(
    session: Session, username: str, *, admin: bool = False, write: bool = False
) -> set[str]:
    """Return the server-derived workspaces this account may read or change."""
    ensure_personal_workspace(session, username)
    if admin:
        return set(session.execute(select(Workspace.id)).scalars())

    owned = set(session.execute(select(Workspace.id).where(Workspace.owner_username == username)).scalars())
    shared_query = select(WorkspaceShare.workspace_id).where(WorkspaceShare.username == username)
    if write:
        shared_query = shared_query.where(WorkspaceShare.permission == "write")
    return owned | set(session.execute(shared_query).scalars())
