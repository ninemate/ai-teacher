import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "common"))
sys.path.insert(0, str(ROOT / "services" / "ingestion" / "app"))
_db_dir = tempfile.mkdtemp(prefix="teacher-tests-")
os.environ["METADATA_DB_URL"] = f"sqlite:///{_db_dir}/teacher.db"
os.environ["INGEST_CONFIG_PATH"] = f"{_db_dir}/none.yml"
