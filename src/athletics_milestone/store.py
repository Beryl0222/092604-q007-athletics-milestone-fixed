"""使用 SQLite 保存里程碑认定账本。

账本只追加、不改写：当年发布的认定与赛后裁决分别落表，
编号由锁定批次经 ``allocations`` 原子占用，中断后凭该表恢复。
"""
import json
import sqlite3
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

-- 赛事届次
CREATE TABLE IF NOT EXISTS editions (
    edition_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    games_no INTEGER,
    year INTEGER NOT NULL,
    opened_at TEXT,
    closed_at TEXT
);
-- 项目规则时代（决定统计序列的规则口径）
CREATE TABLE IF NOT EXISTS rule_eras (
    era_id TEXT PRIMARY KEY,
    discipline TEXT NOT NULL,
    name TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT
);
-- 项目身份：距离/性别/赛制构成规则指纹；指纹变化即新身份，不自动接续
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    discipline TEXT NOT NULL,
    spec TEXT NOT NULL,
    era_id TEXT NOT NULL REFERENCES rule_eras(era_id),
    superseded_by TEXT REFERENCES events(event_id),
    note TEXT
);
-- 运动员身份：同名默认不同人；合并必须留证据
CREATE TABLE IF NOT EXISTS athletes (
    athlete_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    gender TEXT,
    birth_date TEXT,
    merged_into TEXT REFERENCES athletes(athlete_id),
    merged_at TEXT,
    merged_evidence TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    kind TEXT,
    title TEXT,
    publisher TEXT,
    published_at TEXT,
    uri TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(source_id),
    locator TEXT,
    excerpt TEXT
);

-- 导入批次：open -> locked（已占号）-> committing -> committed
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    locked_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS batch_items (
    item_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    seq INTEGER NOT NULL,
    result_id TEXT NOT NULL UNIQUE,
    edition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    winner_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'staged',          -- staged/conflict/done
    milestone_no INTEGER,
    conflict_reason TEXT
);
-- 编号占用表：锁定批次时原子写入，是恢复与发号的唯一依据
CREATE TABLE IF NOT EXISTS allocations (
    batch_id TEXT NOT NULL,
    item_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE,
    milestone_no INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS results (
    result_id TEXT PRIMARY KEY,
    edition_id TEXT NOT NULL REFERENCES editions(edition_id),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    place INTEGER NOT NULL,
    athlete_id TEXT REFERENCES athletes(athlete_id),
    team_code TEXT,
    performance TEXT,
    performance_unit TEXT,
    result_date TEXT NOT NULL,
    source_id TEXT,
    status TEXT NOT NULL DEFAULT 'official',      -- official/reallocated/vacated
    committed_batch TEXT
);
CREATE TABLE IF NOT EXISTS relay_lineups (
    result_id TEXT NOT NULL,
    leg INTEGER NOT NULL,
    athlete_id TEXT NOT NULL,
    PRIMARY KEY (result_id, leg)
);

-- 当晚发布的公开编号认定（永不更新、永不删除）
CREATE TABLE IF NOT EXISTS assertions (
    milestone_no INTEGER PRIMARY KEY,
    result_id TEXT NOT NULL UNIQUE,
    edition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    athlete_id TEXT,
    team_code TEXT,
    valid_from TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    basis_json TEXT NOT NULL
);

-- 赛后裁决以追加的替代关系表达
CREATE TABLE IF NOT EXISTS adjudications (
    adj_id TEXT PRIMARY KEY,
    adjudicated_at TEXT NOT NULL,
    kind TEXT NOT NULL,                            -- vacate/reassign/reinstate
    milestone_no INTEGER NOT NULL REFERENCES assertions(milestone_no),
    target_result_id TEXT,
    to_result_id TEXT,
    to_athlete_id TEXT,
    to_team_code TEXT,
    reason TEXT,
    evidence_id TEXT
);
CREATE TABLE IF NOT EXISTS adjudication_effects (
    effect_id INTEGER PRIMARY KEY AUTOINCREMENT,
    adj_id TEXT NOT NULL REFERENCES adjudications(adj_id),
    milestone_no INTEGER NOT NULL,
    effect TEXT NOT NULL,                          -- vacate/reassign/reinstate
    result_id TEXT,
    athlete_id TEXT,
    team_code TEXT,
    valid_from TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflict_queue (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conflict_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    other_ref TEXT,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',            -- open/resolved
    resolved_at TEXT,
    resolution TEXT
);
-- 硬兜底：同一届次同一项目身份只能有一枚金牌，即使并行检测同时漏判，
-- 提交第二枚时也会被数据库原子拒绝，而不是产生两金。
CREATE UNIQUE INDEX IF NOT EXISTS one_gold_per_event
    ON results(edition_id, event_id) WHERE place=1;
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # ---- 基础登记（既有行为）---------------------------------------------
    def add(self, record: Record) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id,owner_id,state,revision,created_at) VALUES(?,?,?,?,?)",
                (record.record_id, record.owner_id, record.state, record.revision, record.created_at),
            )

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id,owner_id,state,revision,created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ---- 事务与通用 -------------------------------------------------------
    @contextmanager
    def transaction(self):
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()

    def put_receipt(self, key: str, payload_hash: str, response_json: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO request_receipts(request_key,payload_hash,response_json) VALUES(?,?,?)",
                (key, payload_hash, response_json),
            )

    def get_receipt(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT response_json FROM request_receipts WHERE request_key=?", (key,)
        ).fetchone()
        return row["response_json"] if row else None

    # ---- 基础资料 ---------------------------------------------------------
    def put_edition(self, edition_id, name, year, games_no=None, opened_at=None, closed_at=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO editions(edition_id,name,games_no,year,opened_at,closed_at)"
                " VALUES(?,?,?,?,?,?)",
                (edition_id, name, games_no, year, opened_at, closed_at),
            )

    def list_editions(self):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM editions ORDER BY year").fetchall()]

    def put_era(self, era_id, discipline, name, effective_from, effective_to=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO rule_eras(era_id,discipline,name,effective_from,effective_to)"
                " VALUES(?,?,?,?,?)",
                (era_id, discipline, name, effective_from, effective_to),
            )

    def put_event(self, event_id, code, name, discipline, spec, era_id, note=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO events(event_id,code,name,discipline,spec,era_id,note)"
                " VALUES(?,?,?,?,?,?,?)",
                (event_id, code, name, discipline, spec, era_id, note),
            )

    def link_events(self, old_event_id, new_event_id, note=None):
        """记录项目沿革线索：仅供溯源，不构成同一身份。"""
        with self.connection:
            self.connection.execute(
                "UPDATE events SET superseded_by=?, note=COALESCE(?,note) WHERE event_id=?",
                (new_event_id, note, old_event_id),
            )

    def get_event(self, event_id):
        row = self.connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list_events(self):
        return [dict(r) for r in self.connection.execute("SELECT * FROM events").fetchall()]

    def put_athlete(self, athlete_id, display_name, gender=None, birth_date=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO athletes(athlete_id,display_name,gender,birth_date)"
                " VALUES(?,?,?,?)",
                (athlete_id, display_name, gender, birth_date),
            )

    def get_athlete(self, athlete_id):
        row = self.connection.execute("SELECT * FROM athletes WHERE athlete_id=?", (athlete_id,)).fetchone()
        return dict(row) if row else None

    def find_athletes_by_name(self, display_name):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM athletes WHERE display_name=? ORDER BY athlete_id", (display_name,)
        ).fetchall()]

    def merge_athlete(self, from_id, into_id, at, evidence_ref):
        with self.connection:
            self.connection.execute(
                "UPDATE athletes SET merged_into=?, merged_at=?, merged_evidence=? WHERE athlete_id=?",
                (into_id, at, evidence_ref, from_id),
            )

    def canonical_athlete(self, athlete_id):
        seen = set()
        current = athlete_id
        while current and current not in seen:
            seen.add(current)
            row = self.connection.execute(
                "SELECT athlete_id, merged_into FROM athletes WHERE athlete_id=?", (current,)
            ).fetchone()
            if not row or not row["merged_into"]:
                break
            current = row["merged_into"]
        return current

    def put_source(self, source_id, kind=None, title=None, publisher=None, published_at=None, uri=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO sources(source_id,kind,title,publisher,published_at,uri)"
                " VALUES(?,?,?,?,?,?)",
                (source_id, kind, title, publisher, published_at, uri),
            )

    def put_evidence(self, evidence_id, source_id=None, locator=None, excerpt=None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO evidence(evidence_id,source_id,locator,excerpt)"
                " VALUES(?,?,?,?)",
                (evidence_id, source_id, locator, excerpt),
            )

    def evidence_exists(self, evidence_id):
        return self.connection.execute(
            "SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone() is not None

    # ---- 批次与暂存项 -----------------------------------------------------
    def create_batch(self, batch_id, at, note=None):
        with self.connection:
            self.connection.execute(
                "INSERT INTO batches(batch_id,state,created_at,note) VALUES(?,?,?,?)",
                (batch_id, "open", at, note),
            )

    def get_batch(self, batch_id):
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def set_batch_state(self, batch_id, state, at=None):
        with self.connection:
            if state == "locked":
                self.connection.execute(
                    "UPDATE batches SET state=?, locked_at=? WHERE batch_id=?", (state, at, batch_id))
            else:
                self.connection.execute(
                    "UPDATE batches SET state=? WHERE batch_id=?", (state, batch_id))

    def add_item(self, item):
        with self.connection:
            self.connection.execute(
                "INSERT INTO batch_items(item_id,batch_id,seq,result_id,edition_id,event_id,"
                "winner_key,payload_json,state) VALUES(?,?,?,?,?,?,?,?,?)",
                (item["item_id"], item["batch_id"], item["seq"], item["result_id"],
                 item["edition_id"], item["event_id"], item["winner_key"],
                 json.dumps(item["payload"], ensure_ascii=False), item.get("state", "staged")),
            )

    def get_item(self, item_id):
        row = self.connection.execute("SELECT * FROM batch_items WHERE item_id=?", (item_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    def item_by_result(self, result_id):
        row = self.connection.execute(
            "SELECT * FROM batch_items WHERE result_id=?", (result_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    def items_of_batch(self, batch_id):
        rows = self.connection.execute(
            "SELECT * FROM batch_items WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            out.append(data)
        return out

    def mark_item(self, item_id, state, reason=None, milestone_no=None):
        with self.connection:
            self.connection.execute(
                "UPDATE batch_items SET state=?, conflict_reason=?, milestone_no=COALESCE(?,milestone_no)"
                " WHERE item_id=?",
                (state, reason, milestone_no, item_id),
            )

    def committed_gold(self, edition_id, event_id):
        row = self.connection.execute(
            "SELECT * FROM results WHERE edition_id=? AND event_id=? AND place=1",
            (edition_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def pending_gold_items(self, edition_id, event_id, exclude_batch=None):
        sql = ("SELECT * FROM batch_items WHERE edition_id=? AND event_id=? AND state IN ('staged','done')")
        args: list = [edition_id, event_id]
        if exclude_batch:
            sql += " AND batch_id<>?"
            args.append(exclude_batch)
        rows = self.connection.execute(sql, args).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            out.append(data)
        return out

    # ---- 编号占用 ---------------------------------------------------------
    def max_milestone_no(self):
        row = self.connection.execute("SELECT MAX(milestone_no) AS m FROM allocations").fetchone()
        return row["m"] or 0

    def allocations_of_batch(self, batch_id):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM allocations WHERE batch_id=? ORDER BY milestone_no", (batch_id,)
        ).fetchall()]

    def allocation_by_item(self, item_id):
        row = self.connection.execute(
            "SELECT * FROM allocations WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    # ---- 结果与认定 -------------------------------------------------------
    def insert_result(self, r):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO results(result_id,edition_id,event_id,place,athlete_id,"
                "team_code,performance,performance_unit,result_date,source_id,status,committed_batch)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["result_id"], r["edition_id"], r["event_id"], r.get("place", 1),
                 r.get("athlete_id"), r.get("team_code"), r.get("performance"),
                 r.get("performance_unit"), r["result_date"], r.get("source_id"),
                 r.get("status", "official"), r.get("committed_batch")),
            )
            self.connection.execute("DELETE FROM relay_lineups WHERE result_id=?", (r["result_id"],))
            self.connection.executemany(
                "INSERT INTO relay_lineups(result_id,leg,athlete_id) VALUES(?,?,?)",
                [(r["result_id"], leg + 1, aid) for leg, aid in enumerate(r.get("lineup", []))],
            )

    def result_exists(self, result_id):
        return self.connection.execute(
            "SELECT 1 FROM results WHERE result_id=?", (result_id,)).fetchone() is not None

    def get_result(self, result_id):
        row = self.connection.execute("SELECT * FROM results WHERE result_id=?", (result_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["lineup"] = [r["athlete_id"] for r in self.connection.execute(
            "SELECT athlete_id FROM relay_lineups WHERE result_id=? ORDER BY leg", (result_id,))]
        return data

    def lineup_of(self, result_id):
        return [r["athlete_id"] for r in self.connection.execute(
            "SELECT athlete_id FROM relay_lineups WHERE result_id=? ORDER BY leg", (result_id,))]

    def get_assertion(self, milestone_no):
        row = self.connection.execute(
            "SELECT * FROM assertions WHERE milestone_no=?", (milestone_no,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["basis"] = json.loads(data.pop("basis_json"))
        return data

    def assertions_as_of(self, as_of):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM assertions WHERE valid_from<=? ORDER BY milestone_no", (as_of,))]

    # ---- 裁决与冲突 -------------------------------------------------------
    def insert_adjudication(self, adj):
        with self.connection:
            self.connection.execute(
                "INSERT INTO adjudications(adj_id,adjudicated_at,kind,milestone_no,target_result_id,"
                "to_result_id,to_athlete_id,to_team_code,reason,evidence_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (adj["adj_id"], adj["adjudicated_at"], adj["kind"], adj["milestone_no"],
                 adj.get("target_result_id"), adj.get("to_result_id"), adj.get("to_athlete_id"),
                 adj.get("to_team_code"), adj.get("reason"), adj.get("evidence_id")),
            )
            for e in adj["effects"]:
                self.connection.execute(
                    "INSERT INTO adjudication_effects(adj_id,milestone_no,effect,result_id,"
                    "athlete_id,team_code,valid_from) VALUES(?,?,?,?,?,?,?)",
                    (adj["adj_id"], e["milestone_no"], e["effect"], e.get("result_id"),
                     e.get("athlete_id"), e.get("team_code"), adj["adjudicated_at"]),
                )

    def effects_as_of(self, as_of):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM adjudication_effects WHERE valid_from<=?"
            " ORDER BY valid_from, effect_id", (as_of,)).fetchall()]

    def enqueue_conflict(self, conflict_key, batch_id, item_id, reason, other_ref=None):
        with self.connection:
            self.connection.execute(
                "INSERT INTO conflict_queue(conflict_key,batch_id,item_id,other_ref,reason)"
                " VALUES(?,?,?,?,?)",
                (conflict_key, batch_id, item_id, other_ref, reason),
            )

    def list_open_conflicts(self):
        return [dict(r) for r in self.connection.execute(
            "SELECT * FROM conflict_queue WHERE state='open' ORDER BY conflict_id").fetchall()]

    def resolve_conflicts_for(self, item_id, at, resolution):
        with self.connection:
            self.connection.execute(
                "UPDATE conflict_queue SET state='resolved', resolved_at=?, resolution=?"
                " WHERE item_id=? AND state='open'",
                (at, resolution, item_id),
            )
