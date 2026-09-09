import json
import sqlite3
from pathlib import Path

from app.core.models import Mission


DB_PATH = Path("data/yodaw.db")


class MissionStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, mission: Mission):
        payload = mission.model_dump_json()

        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                INSERT INTO missions(id, payload)
                VALUES(?, ?)
                ON CONFLICT(id)
                DO UPDATE SET payload=excluded.payload
                """,
                (mission.id, payload),
            )

    def get(self, mission_id: str) -> Mission | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()

        if not row:
            return None

        return Mission.model_validate_json(row[0])

    def list(self) -> list[Mission]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM missions ORDER BY rowid DESC"
            ).fetchall()

        return [Mission.model_validate_json(row[0]) for row in rows]
