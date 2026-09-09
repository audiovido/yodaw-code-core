import sqlite3
from pathlib import Path

from app.learning.models import LearningRecord


DB_PATH = Path("data/yodaw.db")


class LearningStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_records (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, record: LearningRecord):
        with sqlite3.connect(self.path) as db:
            db.execute(
                """
                INSERT INTO learning_records(id, payload)
                VALUES(?, ?)
                ON CONFLICT(id)
                DO UPDATE SET payload=excluded.payload
                """,
                (record.id, record.model_dump_json()),
            )

    def list(self) -> list[LearningRecord]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM learning_records ORDER BY rowid DESC"
            ).fetchall()

        return [
            LearningRecord.model_validate_json(row[0])
            for row in rows
        ]
