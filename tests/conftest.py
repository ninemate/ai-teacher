import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "common"))
sys.path.insert(0, str(ROOT / "services" / "api" / "app"))

# Must be set before teacher_common.config is first imported (settings are cached).
_tmp = tempfile.mkdtemp(prefix="teacher-tests-")
os.environ["METADATA_DB_URL"] = f"sqlite:///{_tmp}/teacher.db"
os.environ["TEACHER_PROMPT_PATH"] = str(ROOT / "config" / "prompts" / "teacher_hu.md")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture
def session():
    from teacher_common import models, study_models  # noqa: F401
    from teacher_common.db import Base

    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s
