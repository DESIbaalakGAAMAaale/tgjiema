import sys
import tempfile
import types
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rowcount = cursor.rowcount


class _AsyncConnection:
    def __init__(self, path, timeout=10):
        self._conn = sqlite3.connect(path, timeout=timeout, check_same_thread=False)

    async def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        return _AsyncCursor(cursor)

    async def executemany(self, sql, seq_of_params):
        self._conn.executemany(sql, seq_of_params)

    async def execute_fetchall(self, sql, params=()):
        cursor = self._conn.execute(sql, params)
        return cursor.fetchall()

    async def commit(self):
        self._conn.commit()

    async def close(self):
        self._conn.close()


async def _aiosqlite_connect(path, timeout=10):
    return _AsyncConnection(path, timeout=timeout)


_logger = types.SimpleNamespace(
    info=lambda *args, **kwargs: None,
    debug=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)

sys.modules.setdefault("asyncpg", types.SimpleNamespace())
sys.modules.setdefault("loguru", types.SimpleNamespace(logger=_logger))
sys.modules.setdefault("aiosqlite", types.SimpleNamespace(connect=_aiosqlite_connect))

from database import cache_store, session


class _FakeUpdateResult:
    matched_count = 1


class _FakeJobsCol:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.find_calls = []
        self.update_calls = []

    async def find(self, query, **kwargs):
        self.find_calls.append((query, kwargs))
        return list(self.rows)

    async def update_one(self, query, update):
        self.update_calls.append((query, update))
        return _FakeUpdateResult()


class IdleRuRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = cache_store.DB_PATH
        self.original_store = cache_store._store
        cache_store.DB_PATH = Path(self.temp_dir.name) / "cache_store.db"
        cache_store._store = cache_store.CacheStore()
        await cache_store._store.init()
        session._cells_local_cache = None
        session._cells_local_version = 0

    async def asyncTearDown(self):
        await cache_store._store.close()
        cache_store.DB_PATH = self.original_db_path
        cache_store._store = self.original_store
        self.temp_dir.cleanup()

    async def test_sync_local_jobs_to_crdb_marks_done_jobs(self):
        store = cache_store.get_cache_store()
        local_id = await store.insert_local_job(
            {
                "code": "done-job",
                "target_user_id": 1001,
                "storage_channel_id": 2001,
                "storage_msg_ids": "[1]",
                "status": "pending",
                "created_at": "2026-07-01T00:00:00+00:00",
            }
        )
        await store.update_local_job_crdb_id(local_id, 321)
        await store.update_local_job_status(321, "done")

        fake_col = _FakeJobsCol()
        with patch("database.session.get_jobs_col", return_value=fake_col):
            await session.sync_local_jobs_to_crdb()

        self.assertEqual(len(fake_col.update_calls), 1)
        query, update = fake_col.update_calls[0]
        self.assertEqual(query, {"id": 321})
        self.assertEqual(update["$set"]["status"], "done")

        rows = await store._db.execute_fetchall(
            "SELECT synced_at FROM local_job_queue WHERE crdb_id = ?",
            (321,),
        )
        self.assertTrue(rows[0][0] > 0)

    async def test_sync_jobs_from_crdb_to_sqlite_only_reads_pending_jobs(self):
        fake_col = _FakeJobsCol(
            rows=[
                {
                    "id": 999,
                    "code": "pending-job",
                    "target_user_id": 1002,
                    "storage_channel_id": 2002,
                    "storage_msg_ids": "[2]",
                    "batch_file_meta": "",
                    "task_type": "single",
                    "status": "pending",
                    "retry_count": 0,
                    "protect_content": False,
                    "created_at": "2026-07-01T00:00:00+00:00",
                }
            ]
        )
        with patch("database.session.get_jobs_col", return_value=fake_col):
            await session.sync_jobs_from_crdb_to_sqlite(limit=5)

        self.assertEqual(len(fake_col.find_calls), 1)
        query, kwargs = fake_col.find_calls[0]
        self.assertEqual(query, {"status": "pending"})
        self.assertEqual(kwargs["limit"], 5)


if __name__ == "__main__":
    unittest.main()
