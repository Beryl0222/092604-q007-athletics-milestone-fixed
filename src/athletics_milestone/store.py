"""使用 SQLite 保存里程碑账本与幂等请求。

所有写库操作经 write_tx 进入：BEGIN IMMEDIATE 串行化并发写入，
事务整体提交或整体回滚，因此批次中断不会留下半截认定。
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .domain import Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS editions (
    edition_id TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sex TEXT NOT NULL,
    distance TEXT NOT NULL DEFAULT '',
    era_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    reference TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay_lineups (
    lineup_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS relay_members (
    lineup_id TEXT NOT NULL REFERENCES relay_lineups(lineup_id),
    leg INTEGER NOT NULL,
    athlete_id TEXT NOT NULL REFERENCES athletes(athlete_id),
    PRIMARY KEY (lineup_id, leg)
);
CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL REFERENCES editions(edition_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    medal TEXT NOT NULL,
    delegation TEXT NOT NULL,
    holder_kind TEXT NOT NULL,
    athlete_id TEXT REFERENCES athletes(athlete_id),
    lineup_id TEXT REFERENCES relay_lineups(lineup_id),
    holder_key TEXT NOT NULL,
    finalized_on TEXT NOT NULL,
    supersedes TEXT REFERENCES results(result_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_slot ON results(edition_id, event_id, medal);
CREATE TABLE IF NOT EXISTS rulings (
    ruling_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_result_id TEXT REFERENCES results(result_id),
    new_result_id TEXT REFERENCES results(result_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    note TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_cases (
    case_id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    medal TEXT NOT NULL,
    existing_result_id TEXT NOT NULL REFERENCES results(result_id),
    contender_result_id TEXT NOT NULL REFERENCES results(result_id),
    reason TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_resolutions (
    case_id TEXT PRIMARY KEY REFERENCES quarantine_cases(case_id),
    decision TEXT NOT NULL,
    ruling_id TEXT NOT NULL REFERENCES rulings(ruling_id),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS athlete_merges (
    merge_id TEXT PRIMARY KEY,
    merged_athlete_id TEXT NOT NULL REFERENCES athletes(athlete_id),
    survivor_athlete_id TEXT NOT NULL REFERENCES athletes(athlete_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_lineage_links (
    link_id TEXT PRIMARY KEY,
    from_event_id TEXT NOT NULL REFERENCES events(event_id),
    to_event_id TEXT NOT NULL REFERENCES events(event_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sequences (
    sequence_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    delegation TEXT NOT NULL,
    medal TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sequence_editions (
    sequence_id TEXT NOT NULL REFERENCES sequences(sequence_id),
    edition_id TEXT NOT NULL REFERENCES editions(edition_id),
    PRIMARY KEY (sequence_id, edition_id)
);
CREATE TABLE IF NOT EXISTS sequence_events (
    sequence_id TEXT NOT NULL REFERENCES sequences(sequence_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    PRIMARY KEY (sequence_id, event_id)
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    sequence_id TEXT NOT NULL REFERENCES sequences(sequence_id),
    request_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_events (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, state)
);
CREATE TABLE IF NOT EXISTS batch_slots (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    edition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    medal TEXT NOT NULL,
    PRIMARY KEY (batch_id, edition_id, event_id, medal)
);
CREATE TABLE IF NOT EXISTS recognitions (
    sequence_id TEXT NOT NULL REFERENCES sequences(sequence_id),
    number INTEGER NOT NULL,
    edition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    medal TEXT NOT NULL,
    published_result_id TEXT NOT NULL REFERENCES results(result_id),
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (sequence_id, number),
    UNIQUE (sequence_id, edition_id, event_id, medal)
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        # isolation_level=None：事务完全由 write_tx 显式管理。
        self.connection = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._write_lock = threading.RLock()
        self.connection.executescript(SCHEMA)

    @contextmanager
    def write_tx(self):
        """写事务：同一时刻只允许一个写入者，提交或回滚是原子的。"""
        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = self.connection.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---- 骨架保留的通用登记 ----
    def add(self, record: Record) -> None:
        self.execute(
            "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
            (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
        )

    def get(self, record_id: str) -> Record | None:
        row = self.query_one(
            "SELECT record_id,owner_id,state,revision,created_at FROM records WHERE record_id=?",
            (record_id,),
        )
        return Record(**row) if row else None

    # ---- 幂等回执 ----
    def get_receipt(self, request_key: str) -> dict | None:
        return self.query_one(
            "SELECT request_key,payload_hash,response_json FROM request_receipts WHERE request_key=?",
            (request_key,),
        )

    def put_receipt(self, request_key: str, payload_hash: str, response_json: str) -> None:
        self.execute(
            "INSERT INTO request_receipts(request_key,payload_hash,response_json) VALUES(?,?,?)",
            (request_key, payload_hash, response_json),
        )
