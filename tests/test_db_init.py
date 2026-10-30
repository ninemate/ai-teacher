"""api and ingestion both call init_db() at startup and share one SQLite file, so on a fresh volume they
create the tables at the same moment."""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Each process waits for the same start time, then creates the schema.
_STARTUP = """
import sys, time
sys.path.insert(0, sys.argv[2])
from teacher_common.db import init_db
while time.time() < float(sys.argv[1]):
    pass
init_db()
"""


def test_services_starting_together_on_a_fresh_database(tmp_path):
    for round_ in range(4):
        env = {**os.environ, "METADATA_DB_URL": f"sqlite:///{tmp_path}/fresh-{round_}.db"}
        start_at = str(time.time() + 1.5)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _STARTUP, start_at, str(ROOT / "services" / "common")],
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        errors = [err for p in procs if (err := p.communicate(timeout=60)[1]) and p.returncode != 0]
        assert not errors, errors[0]
