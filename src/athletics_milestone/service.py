"""里程碑认定账的应用服务。

账本不变量：
- 结果版本、裁决、身份合并与项目谱系链接只追加，不覆盖历史行；
- 公开编号只能由锁定批次在单个写事务内原子分配；
- 同一统计序列内，一个奖牌槽位（届次, 项目, 奖牌）至多持有一个编号；
- 冲突结果先进入隔离区，处置之后才允许进入有效版本链。
"""
import hashlib
import json
import uuid
from collections import Counter

from .clock import Clock
from .domain import (
    LedgerError,
    Recognition,
    Record,
    ResultVersion,
    athlete_holder_key,
    relay_holder_key,
)
from .store import Store

_VALID_MEDALS = ("gold", "silver", "bronze")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ---------- 骨架保留 ----------
    def health(self) -> dict[str, str]:
        return {"service": "athletics_milestone", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str | int]:
        record = Record(record_id, owner_id, "draft", 1, self.clock.now())
        self.store.add(record)
        return record.__dict__.copy()

    def find(self, record_id: str) -> dict[str, str | int] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    # ---------- 基础资料登记 ----------
    def add_edition(self, edition_id: str, year: int, name: str) -> dict:
        row = self.store.query_one("SELECT * FROM editions WHERE edition_id=?", (edition_id,))
        if row is not None:
            if row["year"] != year or row["name"] != name:
                raise LedgerError("edition_conflict", "届次编号已被不同内容占用")
            return row
        with self.store.write_tx():
            self.store.execute(
                "INSERT INTO editions(edition_id,year,name) VALUES(?,?,?)",
                (edition_id, year, name),
            )
        return {"edition_id": edition_id, "year": year, "name": name}

    def add_event(self, event_id: str, name: str, sex: str,
                  distance: str = "", era_label: str = "") -> dict:
        row = self.store.query_one("SELECT * FROM events WHERE event_id=?", (event_id,))
        if row is not None:
            if (row["name"], row["sex"], row["distance"], row["era_label"]) != (name, sex, distance, era_label):
                raise LedgerError("event_conflict", "项目编号已被不同内容占用")
            return row
        with self.store.write_tx():
            self.store.execute(
                "INSERT INTO events(event_id,name,sex,distance,era_label) VALUES(?,?,?,?,?)",
                (event_id, name, sex, distance, era_label),
            )
        return {"event_id": event_id, "name": name, "sex": sex,
                "distance": distance, "era_label": era_label}

    def add_athlete(self, athlete_id: str, name: str) -> dict:
        # 同名不合并：同名运动员各自持有身份，合并必须走 merge_athletes 并附证据。
        row = self.store.query_one("SELECT * FROM athletes WHERE athlete_id=?", (athlete_id,))
        if row is not None:
            if row["name"] != name:
                raise LedgerError("athlete_conflict", "运动员编号已被不同姓名占用")
            return row
        with self.store.write_tx():
            self.store.execute(
                "INSERT INTO athletes(athlete_id,name) VALUES(?,?)", (athlete_id, name))
        return {"athlete_id": athlete_id, "name": name}

    def add_evidence(self, evidence_id: str, kind: str, title: str, reference: str) -> dict:
        row = self.store.query_one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
        if row is not None:
            if (row["kind"], row["title"], row["reference"]) != (kind, title, reference):
                raise LedgerError("evidence_conflict", "证据编号已被不同内容占用")
            return row
        with self.store.write_tx():
            self.store.execute(
                "INSERT INTO evidence(evidence_id,kind,title,reference,recorded_at) VALUES(?,?,?,?,?)",
                (evidence_id, kind, title, reference, self.clock.now()),
            )
        return {"evidence_id": evidence_id, "kind": kind, "title": title, "reference": reference}

    # ---------- 统计序列 ----------
    def define_sequence(self, sequence_id: str, name: str, delegation: str, medal: str,
                        edition_ids=(), event_ids=()) -> dict:
        """定义统计序列：代表团 + 奖牌种类 + 届次集合 + 项目（规则时代）集合。"""
        delegation = str(delegation).upper()
        medal = str(medal).lower()
        if medal not in _VALID_MEDALS:
            raise LedgerError("invalid_medal", f"不支持的奖牌种类：{medal}")
        existing = self.store.query_one(
            "SELECT * FROM sequences WHERE sequence_id=?", (sequence_id,))
        if existing is not None:
            if (existing["name"], existing["delegation"], existing["medal"]) != (name, delegation, medal):
                raise LedgerError("sequence_conflict", "序列编号已被不同定义占用")
            return self._sequence_summary(sequence_id)
        for edition_id in edition_ids:
            self._require_row("SELECT * FROM editions WHERE edition_id=?", (edition_id,),
                              "edition_not_found", f"届次未登记：{edition_id}")
        for event_id in event_ids:
            self._require_row("SELECT * FROM events WHERE event_id=?", (event_id,),
                              "event_not_found", f"项目未登记：{event_id}")
        with self.store.write_tx():
            self.store.execute(
                "INSERT INTO sequences(sequence_id,name,delegation,medal,created_at) VALUES(?,?,?,?,?)",
                (sequence_id, name, delegation, medal, self.clock.now()),
            )
            for edition_id in edition_ids:
                self.store.execute(
                    "INSERT INTO sequence_editions(sequence_id,edition_id) VALUES(?,?)",
                    (sequence_id, edition_id))
            for event_id in event_ids:
                self.store.execute(
                    "INSERT INTO sequence_events(sequence_id,event_id) VALUES(?,?)",
                    (sequence_id, event_id))
        return self._sequence_summary(sequence_id)

    def sequence_add_edition(self, sequence_id: str, edition_id: str) -> dict:
        self._require_sequence(sequence_id)
        self._require_row("SELECT * FROM editions WHERE edition_id=?", (edition_id,),
                          "edition_not_found", f"届次未登记：{edition_id}")
        with self.store.write_tx():
            self.store.execute(
                "INSERT OR IGNORE INTO sequence_editions(sequence_id,edition_id) VALUES(?,?)",
                (sequence_id, edition_id))
        return self._sequence_summary(sequence_id)

    def sequence_add_event(self, sequence_id: str, event_id: str) -> dict:
        self._require_sequence(sequence_id)
        self._require_row("SELECT * FROM events WHERE event_id=?", (event_id,),
                          "event_not_found", f"项目未登记：{event_id}")
        with self.store.write_tx():
            self.store.execute(
                "INSERT OR IGNORE INTO sequence_events(sequence_id,event_id) VALUES(?,?)",
                (sequence_id, event_id))
        return self._sequence_summary(sequence_id)

    def _sequence_summary(self, sequence_id: str) -> dict:
        seq = self._require_sequence(sequence_id)
        editions, events = self._sequence_scope(sequence_id)
        return {"sequence_id": sequence_id, "name": seq["name"],
                "delegation": seq["delegation"], "medal": seq["medal"],
                "editions": sorted(editions), "events": sorted(events)}

    # ---------- 结果导入（幂等 + 冲突隔离） ----------
    def import_results(self, source: str, request_key: str, results: list[dict]) -> dict:
        """导入一批结果。同一请求键重放返回原响应；键被不同负载占用则报错。

        一次请求要么全部入库要么整体回滚，重试不会产生半截数据。
        """
        if not request_key:
            raise LedgerError("request_key_required", "导入必须携带请求键以保证幂等")
        payload = {"source": source, "results": results}
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self.store.write_tx():
            receipt = self.store.get_receipt(request_key)
            if receipt is not None:
                if receipt["payload_hash"] != digest:
                    raise LedgerError("request_conflict", "请求键已被不同负载占用")
                return json.loads(receipt["response_json"])
            outcomes = [self._import_one(source, item) for item in results]
            response = {"source": source, "request_key": request_key, "outcomes": outcomes}
            self.store.put_receipt(
                request_key, digest, json.dumps(response, ensure_ascii=False, sort_keys=True))
            return response

    def _import_one(self, source: str, item: dict) -> dict:
        now = self.clock.now()
        edition_id = str(item.get("edition_id", ""))
        event_id = str(item.get("event_id", ""))
        self._require_row("SELECT * FROM editions WHERE edition_id=?", (edition_id,),
                          "edition_not_found", f"届次未登记：{edition_id}")
        self._require_row("SELECT * FROM events WHERE event_id=?", (event_id,),
                          "event_not_found", f"项目未登记：{event_id}")
        evidence_id = str(item.get("evidence_id", ""))
        self._require_row("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,),
                          "evidence_not_found", f"证据未登记：{evidence_id}")
        medal = str(item.get("medal", "")).lower()
        if medal not in _VALID_MEDALS:
            raise LedgerError("invalid_medal", f"不支持的奖牌种类：{medal}")
        delegation = str(item.get("delegation", "")).upper()
        if not delegation:
            raise LedgerError("invalid_delegation", "缺少代表团")
        finalized_on = str(item.get("finalized_on", ""))
        if not finalized_on:
            raise LedgerError("invalid_finalized_on", "缺少决赛时间")
        holder_kind, athlete_id, lineup_id, holder_key = self._prepare_holder(item)
        slot = (edition_id, event_id, medal)

        result_id = str(item.get("result_id") or _new_id("res"))
        existing = self._get_result(result_id)
        if existing is not None:
            if existing.slot == slot and existing.holder_key == holder_key:
                return {"status": "duplicate", "result_id": result_id}
            raise LedgerError("result_conflict", "结果编号已被不同内容占用")

        leaf = self._clean_leaf(*slot)
        if leaf is None:
            self._insert_result(result_id, *slot, delegation, holder_kind, athlete_id,
                                lineup_id, holder_key, finalized_on, None, evidence_id,
                                source, now)
            return {"status": "inserted", "result_id": result_id}
        if leaf.holder_key == holder_key and leaf.delegation == delegation:
            return {"status": "duplicate", "result_id": leaf.result_id}

        # 同一槽位已有其他有效持有人：矛盾结果先隔离，等待裁决处置。
        open_case = self.store.query_one(
            """SELECT qc.case_id, qc.contender_result_id
               FROM quarantine_cases qc
               JOIN results c ON c.result_id = qc.contender_result_id
               LEFT JOIN quarantine_resolutions qr ON qr.case_id = qc.case_id
               WHERE qc.edition_id=? AND qc.event_id=? AND qc.medal=?
                 AND qr.case_id IS NULL AND c.holder_key=?""",
            (*slot, holder_key))
        if open_case is not None:
            return {"status": "quarantined", "case_id": open_case["case_id"],
                    "result_id": open_case["contender_result_id"]}
        self._insert_result(result_id, *slot, delegation, holder_kind, athlete_id,
                            lineup_id, holder_key, finalized_on, None, evidence_id,
                            source, now)
        case_id = _new_id("case")
        self.store.execute(
            """INSERT INTO quarantine_cases(case_id,edition_id,event_id,medal,
               existing_result_id,contender_result_id,reason,source,recorded_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (case_id, *slot, leaf.result_id, result_id,
             "导入持有人与当前有效结果冲突", source, now))
        return {"status": "quarantined", "case_id": case_id, "result_id": result_id}

    def _prepare_holder(self, item: dict):
        if "athlete_id" in item and "lineup" in item:
            raise LedgerError("invalid_holder", "持有人只能是个人或接力阵容之一")
        if "athlete_id" in item:
            athlete_id = str(item["athlete_id"])
            self._require_row("SELECT * FROM athletes WHERE athlete_id=?", (athlete_id,),
                              "athlete_not_found", f"运动员未登记：{athlete_id}")
            return "athlete", athlete_id, None, athlete_holder_key(athlete_id)
        lineup = item.get("lineup")
        if not lineup:
            raise LedgerError("holder_required", "缺少奖牌持有人")
        members = [(int(m["leg"]), str(m["athlete_id"])) for m in lineup.get("members", [])]
        if not members:
            raise LedgerError("invalid_lineup", "接力阵容不能为空")
        legs = [leg for leg, _ in members]
        if len(set(legs)) != len(legs):
            raise LedgerError("invalid_lineup", "接力棒次重复")
        for _, athlete_id in members:
            self._require_row("SELECT * FROM athletes WHERE athlete_id=?", (athlete_id,),
                              "athlete_not_found", f"运动员未登记：{athlete_id}")
        lineup_id = str(lineup.get("lineup_id") or _new_id("lineup"))
        recorded = self.store.query_one(
            "SELECT lineup_id FROM relay_lineups WHERE lineup_id=?", (lineup_id,))
        if recorded is None:
            self.store.execute("INSERT INTO relay_lineups(lineup_id) VALUES(?)", (lineup_id,))
            for leg, athlete_id in sorted(members):
                self.store.execute(
                    "INSERT INTO relay_members(lineup_id,leg,athlete_id) VALUES(?,?,?)",
                    (lineup_id, leg, athlete_id))
        elif sorted(self._lineup_members(lineup_id)) != sorted(members):
            raise LedgerError("lineup_conflict", "阵容编号已被不同成员占用")
        return "relay", None, lineup_id, relay_holder_key(members)

    def _insert_result(self, result_id, edition_id, event_id, medal, delegation,
                       holder_kind, athlete_id, lineup_id, holder_key, finalized_on,
                       supersedes, evidence_id, source, recorded_at) -> None:
        self.store.execute(
            """INSERT INTO results(result_id,edition_id,event_id,medal,delegation,
               holder_kind,athlete_id,lineup_id,holder_key,finalized_on,supersedes,
               evidence_id,source,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (result_id, edition_id, event_id, medal, delegation, holder_kind, athlete_id,
             lineup_id, holder_key, finalized_on, supersedes, evidence_id, source, recorded_at))

    # ---------- 批次编号 ----------
    def open_batch(self, sequence_id: str, request_key: str) -> dict:
        self._require_sequence(sequence_id)
        if not request_key:
            raise LedgerError("request_key_required", "开批次必须携带请求键以保证幂等")
        with self.store.write_tx():
            row = self.store.query_one(
                "SELECT batch_id FROM batches WHERE request_key=?", (request_key,))
            if row is not None:
                return {"batch_id": row["batch_id"], "sequence_id": sequence_id, "reused": True}
            batch_id = _new_id("batch")
            self.store.execute(
                "INSERT INTO batches(batch_id,sequence_id,request_key,created_at) VALUES(?,?,?,?)",
                (batch_id, sequence_id, request_key, self.clock.now()))
            return {"batch_id": batch_id, "sequence_id": sequence_id, "reused": False}

    def lock_batch(self, batch_id: str) -> dict:
        """锁定批次：冻结当前有效且尚未认定的槽位集合。"""
        with self.store.write_tx():
            batch = self._require_batch(batch_id)
            states = self._batch_states(batch_id)
            if "committed" in states:
                raise LedgerError("batch_committed", "批次已提交，不能再锁定")
            if "locked" in states:
                count = self.store.query_one(
                    "SELECT COUNT(*) AS c FROM batch_slots WHERE batch_id=?",
                    (batch_id,))["c"]
                return {"batch_id": batch_id, "locked_slots": count, "replayed": True}
            effective = self._effective_slots(batch["sequence_id"])
            recognized = self._recognized_slots(batch["sequence_id"])
            now = self.clock.now()
            count = 0
            for slot, _leaf in effective:
                if slot in recognized:
                    continue
                self.store.execute(
                    "INSERT INTO batch_slots(batch_id,edition_id,event_id,medal) VALUES(?,?,?,?)",
                    (batch_id, *slot))
                count += 1
            self.store.execute(
                "INSERT INTO batch_events(batch_id,state,recorded_at) VALUES(?,?,?)",
                (batch_id, "locked", now))
            return {"batch_id": batch_id, "locked_slots": count, "replayed": False}

    def commit_batch(self, batch_id: str) -> dict:
        """提交批次：在单个事务内为锁定槽位原子分配公开编号。

        编号从序列当前最大值续排；同一槽位在全序列至多一个编号。
        已提交的批次重复提交是幂等重放。
        """
        with self.store.write_tx():
            batch = self._require_batch(batch_id)
            states = self._batch_states(batch_id)
            if "committed" in states:
                return {"batch_id": batch_id,
                        "assignments": self._batch_assignments(batch_id), "replayed": True}
            if "locked" not in states:
                raise LedgerError("batch_not_locked", "批次尚未锁定，不能分配编号")
            sequence_id = batch["sequence_id"]
            effective = dict(self._effective_slots(sequence_id))
            recognized = self._recognized_slots(sequence_id)
            slots = [(r["edition_id"], r["event_id"], r["medal"])
                     for r in self.store.query(
                         "SELECT edition_id,event_id,medal FROM batch_slots WHERE batch_id=?",
                         (batch_id,))]
            slots.sort(key=lambda s: (
                effective[s].finalized_on if s in effective else "9999", s[1], s[0]))
            number = self.store.query_one(
                "SELECT COALESCE(MAX(number),0) AS m FROM recognitions WHERE sequence_id=?",
                (sequence_id,))["m"]
            now = self.clock.now()
            assignments = []
            for slot in slots:
                if slot in recognized:
                    continue  # 已被其他批次认定：一枚奖牌不能抢到两个编号
                leaf = effective.get(slot)
                if leaf is None:
                    continue  # 锁定后被剥夺或隔离的槽位不再授予编号
                number += 1
                self._insert_recognition(sequence_id, number, slot,
                                         leaf.result_id, batch_id, now)
                recognized.add(slot)
                assignments.append({"number": number, "edition_id": slot[0],
                                    "event_id": slot[1], "medal": slot[2],
                                    "result_id": leaf.result_id})
            self.store.execute(
                "INSERT INTO batch_events(batch_id,state,recorded_at) VALUES(?,?,?)",
                (batch_id, "committed", now))
            return {"batch_id": batch_id, "assignments": assignments, "replayed": False}

    def recover_batches(self) -> dict:
        """从账本恢复中断的批次：锁定而未提交的批次重新执行提交。

        提交是单事务，崩溃只会留下“锁定未提交”状态，重跑提交即可，
        且提交本身幂等，重复恢复不会重复编号。
        """
        rows = self.store.query(
            """SELECT b.batch_id FROM batches b
               WHERE EXISTS (SELECT 1 FROM batch_events e
                             WHERE e.batch_id=b.batch_id AND e.state='locked')
                 AND NOT EXISTS (SELECT 1 FROM batch_events e
                                 WHERE e.batch_id=b.batch_id AND e.state='committed')""")
        recovered = []
        for row in rows:
            result = self.commit_batch(row["batch_id"])
            recovered.append({"batch_id": row["batch_id"],
                              "assignments": result["assignments"]})
        return {"recovered": recovered}

    def _insert_recognition(self, sequence_id, number, slot,
                            published_result_id, batch_id, recorded_at) -> None:
        self.store.execute(
            """INSERT INTO recognitions(sequence_id,number,edition_id,event_id,medal,
               published_result_id,batch_id,recorded_at) VALUES(?,?,?,?,?,?,?,?)""",
            (sequence_id, number, slot[0], slot[1], slot[2],
             published_result_id, batch_id, recorded_at))

    def _require_batch(self, batch_id: str) -> dict:
        return self._require_row("SELECT * FROM batches WHERE batch_id=?", (batch_id,),
                                 "batch_not_found", f"批次不存在：{batch_id}")

    def _batch_states(self, batch_id: str) -> set:
        return {r["state"] for r in self.store.query(
            "SELECT state FROM batch_events WHERE batch_id=?", (batch_id,))}

    def _batch_assignments(self, batch_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM recognitions WHERE batch_id=? ORDER BY number", (batch_id,))
        return [{"number": r["number"], "edition_id": r["edition_id"],
                 "event_id": r["event_id"], "medal": r["medal"],
                 "result_id": r["published_result_id"]} for r in rows]

    def _recognized_slots(self, sequence_id: str) -> set:
        return {(r["edition_id"], r["event_id"], r["medal"])
                for r in self.store.query(
                    "SELECT edition_id,event_id,medal FROM recognitions WHERE sequence_id=?",
                    (sequence_id,))}

    # ---------- 赛后裁决（只追加替代关系） ----------
    def record_ruling(self, kind: str, evidence_id: str, note: str = "", **payload) -> dict:
        """登记裁决。取消资格追加剥夺标记，重分配追加新版本；当年记录不覆盖。"""
        self._require_row("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,),
                          "evidence_not_found", f"裁决必须附证据出处，未登记：{evidence_id}")
        now = self.clock.now()
        with self.store.write_tx():
            if kind == "disqualification":
                result_id = str(payload.get("result_id", ""))
                target = self._require_result(result_id)
                ruling_id = _new_id("rule")
                self.store.execute(
                    """INSERT INTO rulings(ruling_id,kind,target_result_id,new_result_id,
                       evidence_id,note,recorded_at) VALUES(?,?,?,?,?,?,?)""",
                    (ruling_id, kind, target.result_id, None, evidence_id, note, now))
                return {"ruling_id": ruling_id, "kind": kind,
                        "target_result_id": target.result_id}
            if kind == "reallocation":
                if payload.get("result_id"):
                    leaf = self._require_result(str(payload["result_id"]))
                    slot = leaf.slot
                else:
                    slot = (str(payload.get("edition_id", "")),
                            str(payload.get("event_id", "")),
                            str(payload.get("medal", "")).lower())
                    leaf = self._clean_leaf(*slot)
                    if leaf is None:
                        raise LedgerError("slot_empty", "该槽位没有有效结果可重分配")
                holder_kind, athlete_id, lineup_id, holder_key = self._prepare_holder(payload)
                delegation = str(payload.get("delegation") or leaf.delegation).upper()
                finalized_on = str(payload.get("finalized_on") or leaf.finalized_on)
                new_result_id = _new_id("res")
                self._insert_result(new_result_id, *slot, delegation, holder_kind,
                                    athlete_id, lineup_id, holder_key, finalized_on,
                                    leaf.result_id, evidence_id, "ruling", now)
                ruling_id = _new_id("rule")
                self.store.execute(
                    """INSERT INTO rulings(ruling_id,kind,target_result_id,new_result_id,
                       evidence_id,note,recorded_at) VALUES(?,?,?,?,?,?,?)""",
                    (ruling_id, kind, leaf.result_id, new_result_id, evidence_id, note, now))
                return {"ruling_id": ruling_id, "kind": kind,
                        "target_result_id": leaf.result_id,
                        "new_result_id": new_result_id}
            raise LedgerError("invalid_ruling", f"不支持的裁决类型：{kind}")

    def resolve_quarantine(self, case_id: str, decision: str,
                           evidence_id: str, note: str = "") -> dict:
        """处置隔离案件：维持现状，或接受争议主张并追加替代版本。"""
        self._require_row("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,),
                          "evidence_not_found", f"处置必须附证据出处，未登记：{evidence_id}")
        now = self.clock.now()
        with self.store.write_tx():
            case = self._require_row(
                "SELECT * FROM quarantine_cases WHERE case_id=?", (case_id,),
                "case_not_found", f"隔离案件不存在：{case_id}")
            done = self.store.query_one(
                "SELECT * FROM quarantine_resolutions WHERE case_id=?", (case_id,))
            if done is not None:
                return {"case_id": case_id, "decision": done["decision"],
                        "ruling_id": done["ruling_id"], "replayed": True}
            if decision not in ("keep_current", "accept_contender"):
                raise LedgerError("invalid_decision", f"不支持的处置方式：{decision}")
            slot = (case["edition_id"], case["event_id"], case["medal"])
            leaf = self._clean_leaf(*slot)
            new_result_id = None
            if decision == "accept_contender":
                contender = self._require_result(case["contender_result_id"])
                if leaf is None:
                    raise LedgerError("ledger_corrupt", "隔离案件的原始结果缺失")
                new_result_id = _new_id("res")
                self._insert_result(new_result_id, *slot, contender.delegation,
                                    contender.holder_kind, contender.athlete_id,
                                    contender.lineup_id, contender.holder_key,
                                    contender.finalized_on, leaf.result_id,
                                    evidence_id, "ruling", now)
            ruling_id = _new_id("rule")
            self.store.execute(
                """INSERT INTO rulings(ruling_id,kind,target_result_id,new_result_id,
                   evidence_id,note,recorded_at) VALUES(?,?,?,?,?,?,?)""",
                (ruling_id, "quarantine_resolution",
                 leaf.result_id if leaf else case["existing_result_id"],
                 new_result_id, evidence_id, note, now))
            self.store.execute(
                "INSERT INTO quarantine_resolutions(case_id,decision,ruling_id,recorded_at)"
                " VALUES(?,?,?,?)",
                (case_id, decision, ruling_id, now))
            return {"case_id": case_id, "decision": decision, "ruling_id": ruling_id,
                    "new_result_id": new_result_id, "replayed": False}

    def list_quarantine(self, only_open: bool = True) -> list[dict]:
        rows = self.store.query(
            """SELECT qc.*, qr.decision AS decision
               FROM quarantine_cases qc
               LEFT JOIN quarantine_resolutions qr ON qr.case_id = qc.case_id
               ORDER BY qc.recorded_at""")
        cases = []
        for row in rows:
            if only_open and row["decision"] is not None:
                continue
            existing = self._require_result(row["existing_result_id"])
            contender = self._require_result(row["contender_result_id"])
            cases.append({
                "case_id": row["case_id"],
                "edition_id": row["edition_id"],
                "event_id": row["event_id"],
                "medal": row["medal"],
                "existing": {"result_id": existing.result_id,
                             "holder_key": existing.holder_key},
                "contender": {"result_id": contender.result_id,
                              "holder_key": contender.holder_key},
                "reason": row["reason"],
                "source": row["source"],
                "state": "resolved" if row["decision"] else "open",
                "decision": row["decision"],
                "recorded_at": row["recorded_at"],
            })
        return cases

    # ---------- 身份沿革与项目谱系（凭证据追加） ----------
    def merge_athletes(self, merged_athlete_id: str, survivor_athlete_id: str,
                       evidence_id: str) -> dict:
        """合并两个运动员身份。同名不等于同人，必须附证据才能合并。"""
        if merged_athlete_id == survivor_athlete_id:
            raise LedgerError("invalid_merge", "不能把身份合并到自身")
        self._require_row("SELECT * FROM athletes WHERE athlete_id=?", (merged_athlete_id,),
                          "athlete_not_found", f"运动员未登记：{merged_athlete_id}")
        self._require_row("SELECT * FROM athletes WHERE athlete_id=?", (survivor_athlete_id,),
                          "athlete_not_found", f"运动员未登记：{survivor_athlete_id}")
        self._require_row("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,),
                          "evidence_not_found", f"身份合并必须附证据，未登记：{evidence_id}")
        with self.store.write_tx():
            if self.store.query_one(
                    "SELECT merge_id FROM athlete_merges WHERE merged_athlete_id=?",
                    (merged_athlete_id,)) is not None:
                raise LedgerError("already_merged", "该身份已被合并过")
            ancestor = survivor_athlete_id
            seen = {ancestor}
            while True:
                row = self.store.query_one(
                    "SELECT survivor_athlete_id AS s FROM athlete_merges WHERE merged_athlete_id=?",
                    (ancestor,))
                if row is None:
                    break
                ancestor = row["s"]
                if ancestor == merged_athlete_id:
                    raise LedgerError("merge_cycle", "合并会形成身份环")
                if ancestor in seen:
                    break
                seen.add(ancestor)
            merge_id = _new_id("merge")
            self.store.execute(
                """INSERT INTO athlete_merges(merge_id,merged_athlete_id,survivor_athlete_id,
                   evidence_id,recorded_at) VALUES(?,?,?,?,?)""",
                (merge_id, merged_athlete_id, survivor_athlete_id,
                 evidence_id, self.clock.now()))
            return {"merge_id": merge_id, "merged_athlete_id": merged_athlete_id,
                    "survivor_athlete_id": survivor_athlete_id}

    def link_event_lineage(self, from_event_id: str, to_event_id: str,
                           evidence_id: str) -> dict:
        """把两个项目定义连成同一统计项目。更名或距离变化不会自动视同同一项目。"""
        if from_event_id == to_event_id:
            raise LedgerError("invalid_link", "不能把项目连接到自身")
        self._require_row("SELECT * FROM events WHERE event_id=?", (from_event_id,),
                          "event_not_found", f"项目未登记：{from_event_id}")
        self._require_row("SELECT * FROM events WHERE event_id=?", (to_event_id,),
                          "event_not_found", f"项目未登记：{to_event_id}")
        self._require_row("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,),
                          "evidence_not_found", f"项目谱系必须附证据，未登记：{evidence_id}")
        with self.store.write_tx():
            existing = self.store.query_one(
                """SELECT link_id FROM event_lineage_links
                   WHERE (from_event_id=? AND to_event_id=?)
                      OR (from_event_id=? AND to_event_id=?)""",
                (from_event_id, to_event_id, to_event_id, from_event_id))
            if existing is not None:
                return {"link_id": existing["link_id"], "replayed": True}
            link_id = _new_id("link")
            self.store.execute(
                """INSERT INTO event_lineage_links(link_id,from_event_id,to_event_id,
                   evidence_id,recorded_at) VALUES(?,?,?,?,?)""",
                (link_id, from_event_id, to_event_id, evidence_id, self.clock.now()))
            return {"link_id": link_id, "replayed": False}

    # ---------- 查询：当前第 N 枚 / 历史快照 / 统计与差异 ----------
    def milestone(self, sequence_id: str, number: int, as_of: str | None = None) -> dict:
        """第 N 枚奖牌的双重回答：当晚发布的认定 + 当前（或指定日期）有效的认定。"""
        self._require_sequence(sequence_id)
        number = int(number)
        if number < 1:
            raise LedgerError("invalid_number", "编号必须为正整数")
        cutoff = as_of or self.clock.now()

        published = None
        rulings = []
        row = self.store.query_one(
            "SELECT * FROM recognitions WHERE sequence_id=? AND number=? AND recorded_at<=?",
            (sequence_id, number, cutoff))
        if row is not None:
            recognition = Recognition.from_row(row)
            published_result = self._require_result(recognition.published_result_id)
            leaf = self._clean_leaf(*recognition.slot, cutoff)
            stripped = self._stripped_ids(cutoff)
            if leaf is None:
                slot_status = "unknown"
            elif leaf.result_id in stripped:
                slot_status = "stripped"
            elif leaf.result_id != recognition.published_result_id:
                slot_status = "replaced"
            else:
                slot_status = "effective"
            published = {
                "edition_id": recognition.edition_id,
                "event_id": recognition.event_id,
                "medal": recognition.medal,
                "result": self._describe_result(published_result, cutoff),
                "batch_id": recognition.batch_id,
                "recorded_at": recognition.recorded_at,
                "slot_status": slot_status,
            }
            rulings = self._slot_rulings(recognition.slot, recognition.recorded_at, cutoff)

        effective = None
        ordered = self._effective_slots(sequence_id, cutoff)
        if number <= len(ordered):
            slot, leaf = ordered[number - 1]
            pub_row = self.store.query_one(
                """SELECT number FROM recognitions
                   WHERE sequence_id=? AND edition_id=? AND event_id=? AND medal=?
                     AND recorded_at<=?""",
                (sequence_id, *slot, cutoff))
            effective = {
                "edition_id": slot[0], "event_id": slot[1], "medal": slot[2],
                "result": self._describe_result(leaf, cutoff),
                "published_number": pub_row["number"] if pub_row else None,
            }

        consistent = bool(
            published and effective
            and published["slot_status"] == "effective"
            and published["edition_id"] == effective["edition_id"]
            and published["event_id"] == effective["event_id"]
            and published["result"]["result_id"] == effective["result"]["result_id"])
        return {"sequence_id": sequence_id, "number": number, "as_of": cutoff,
                "published": published, "effective": effective,
                "consistent": consistent, "rulings": rulings}

    def snapshot(self, sequence_id: str, as_of: str) -> dict:
        """指定历史日期的认定快照：按当时账本状态重排的有效序列。"""
        if not as_of:
            raise LedgerError("as_of_required", "快照必须指定历史日期")
        self._require_sequence(sequence_id)
        ordered = self._effective_slots(sequence_id, as_of)
        entries = []
        for position, (slot, leaf) in enumerate(ordered, start=1):
            pub_row = self.store.query_one(
                """SELECT number FROM recognitions
                   WHERE sequence_id=? AND edition_id=? AND event_id=? AND medal=?
                     AND recorded_at<=?""",
                (sequence_id, *slot, as_of))
            entries.append({
                "position": position,
                "edition_id": slot[0], "event_id": slot[1], "medal": slot[2],
                "result": self._describe_result(leaf, as_of),
                "published_number": pub_row["number"] if pub_row else None,
            })
        return {"sequence_id": sequence_id, "as_of": as_of,
                "count": len(entries), "entries": entries}

    def stats(self, sequence_id: str, as_of: str | None = None) -> dict:
        """个人、项目（谱系）与代表团届次统计。"""
        self._require_sequence(sequence_id)
        cutoff = as_of or self.clock.now()
        ordered = self._effective_slots(sequence_id, cutoff)
        aggregate = self._aggregate(ordered, cutoff)
        return {
            "sequence_id": sequence_id,
            "as_of": cutoff,
            "total_medals": len(ordered),
            "athletes": [
                {"athlete_id": athlete_id, "name": self._athlete_name(athlete_id),
                 "count": count}
                for athlete_id, count in sorted(aggregate["athletes"].items())],
            "events": [
                {"lineage": root,
                 "event_ids": self._lineage_members(root, cutoff),
                 "names": [self._event_name(e) for e in self._lineage_members(root, cutoff)],
                 "count": count}
                for root, count in sorted(aggregate["events"].items())],
            "editions": [
                {"edition_id": edition_id, "count": count}
                for edition_id, count in sorted(aggregate["editions"].items())],
        }

    def stats_diff(self, sequence_id: str, since: str) -> dict:
        """后续裁决带来的差异：奖牌进出、持有人变化与个人/项目/代表团统计差。"""
        self._require_sequence(sequence_id)
        now = self.clock.now()
        before_ordered = self._effective_slots(sequence_id, since)
        after_ordered = self._effective_slots(sequence_id, now)
        before = self._aggregate(before_ordered, since)
        after = self._aggregate(after_ordered, now)
        before_slots = dict(before_ordered)
        after_slots = dict(after_ordered)
        added = sorted(s for s in after_slots if s not in before_slots)
        removed = sorted(s for s in before_slots if s not in after_slots)
        changed = sorted(s for s in before_slots.keys() & after_slots.keys()
                         if before_slots[s].holder_key != after_slots[s].holder_key)
        return {
            "sequence_id": sequence_id,
            "since": since,
            "until": now,
            "medals_added": [self._slot_summary(slot, after_slots[slot], now)
                             for slot in added],
            "medals_removed": [self._slot_summary(slot, before_slots[slot], since)
                               for slot in removed],
            "holder_changes": [
                {"edition_id": slot[0], "event_id": slot[1], "medal": slot[2],
                 "before": self._describe_holder(before_slots[slot], since),
                 "after": self._describe_holder(after_slots[slot], now)}
                for slot in changed],
            "athlete_delta": self._counter_delta(
                before["athletes"], after["athletes"],
                lambda key: {"athlete_id": key, "name": self._athlete_name(key)}),
            "event_delta": self._counter_delta(
                before["events"], after["events"],
                lambda key: {"lineage": key, "event_ids": self._lineage_members(key)}),
            "edition_delta": self._counter_delta(
                before["editions"], after["editions"],
                lambda key: {"edition_id": key}),
            "rulings": self._sequence_rulings(sequence_id, since, now),
        }

    # ---------- 内部：有效版本链推导 ----------
    def _effective_slots(self, sequence_id: str, cutoff: str | None = None) -> list:
        """序列内当前有效的（槽位, 叶子版本），按（决赛时间, 项目, 届次）规范排序。"""
        seq = self._require_sequence(sequence_id)
        editions, events = self._sequence_scope(sequence_id)
        if not editions or not events:
            return []
        sql = "SELECT * FROM results WHERE delegation=? AND medal=?"
        params = [seq["delegation"], seq["medal"]]
        if cutoff is not None:
            sql += " AND recorded_at<=?"
            params.append(cutoff)
        rows = [ResultVersion.from_row(r) for r in self.store.query(sql, params)]
        rows = [r for r in rows if r.edition_id in editions and r.event_id in events]
        quarantined = self._quarantined_ids(cutoff)
        stripped = self._stripped_ids(cutoff)
        by_slot: dict[tuple, list] = {}
        for row in rows:
            by_slot.setdefault(row.slot, []).append(row)
        ordered = []
        for slot, versions in by_slot.items():
            clean = [v for v in versions if v.result_id not in quarantined]
            superseded = {v.supersedes for v in clean if v.supersedes}
            leaves = [v for v in clean if v.result_id not in superseded]
            if len(leaves) != 1:
                raise LedgerError("ledger_corrupt", f"槽位 {slot} 的有效版本链异常")
            leaf = leaves[0]
            if leaf.result_id in stripped:
                continue
            ordered.append((slot, leaf))
        ordered.sort(key=lambda item: (item[1].finalized_on, item[0][1], item[0][0]))
        return ordered

    def _clean_leaf(self, edition_id: str, event_id: str, medal: str,
                    cutoff: str | None = None) -> ResultVersion | None:
        """槽位有效版本链的叶子；被隔离的争议版本不参与链。"""
        versions = self._slot_results(edition_id, event_id, medal, cutoff)
        quarantined = self._quarantined_ids(cutoff)
        clean = [v for v in versions if v.result_id not in quarantined]
        superseded = {v.supersedes for v in clean if v.supersedes}
        leaves = [v for v in clean if v.result_id not in superseded]
        if len(leaves) > 1:
            raise LedgerError("ledger_corrupt", "有效版本链出现多个叶子，需要人工核查")
        return leaves[0] if leaves else None

    def _slot_results(self, edition_id: str, event_id: str, medal: str,
                      cutoff: str | None = None) -> list[ResultVersion]:
        sql = "SELECT * FROM results WHERE edition_id=? AND event_id=? AND medal=?"
        params = [edition_id, event_id, medal]
        if cutoff is not None:
            sql += " AND recorded_at<=?"
            params.append(cutoff)
        return [ResultVersion.from_row(r) for r in self.store.query(sql, params)]

    def _quarantined_ids(self, cutoff: str | None) -> set:
        sql = "SELECT contender_result_id AS rid FROM quarantine_cases"
        params = []
        if cutoff is not None:
            sql += " WHERE recorded_at<=?"
            params.append(cutoff)
        return {r["rid"] for r in self.store.query(sql, params)}

    def _stripped_ids(self, cutoff: str | None) -> set:
        sql = "SELECT target_result_id AS rid FROM rulings WHERE kind='disqualification'"
        params = []
        if cutoff is not None:
            sql += " AND recorded_at<=?"
            params.append(cutoff)
        return {r["rid"] for r in self.store.query(sql, params)}

    def _slot_rulings(self, slot: tuple, since: str, cutoff: str) -> list[dict]:
        ids = [r.result_id for r in self._slot_results(*slot, cutoff)]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.store.query(
            f"""SELECT * FROM rulings
                WHERE (target_result_id IN ({marks}) OR new_result_id IN ({marks}))
                  AND recorded_at>? AND recorded_at<=?
                ORDER BY recorded_at""",
            (*ids, *ids, since, cutoff))

    def _sequence_rulings(self, sequence_id: str, since: str, until: str) -> list[dict]:
        seq = self._require_sequence(sequence_id)
        editions, events = self._sequence_scope(sequence_id)
        rows = self.store.query(
            "SELECT * FROM rulings WHERE recorded_at>? AND recorded_at<=?"
            " ORDER BY recorded_at",
            (since, until))
        relevant = []
        for row in rows:
            for result_id in (row["target_result_id"], row["new_result_id"]):
                if not result_id:
                    continue
                result = self._get_result(result_id)
                if (result and result.edition_id in editions
                        and result.event_id in events
                        and result.medal == seq["medal"]
                        and result.delegation == seq["delegation"]):
                    relevant.append(row)
                    break
        return relevant

    # ---------- 内部：身份与谱系解析 ----------
    def _resolve_athlete(self, athlete_id: str, cutoff: str | None = None) -> str:
        """沿合并链解析当前身份；快照只应用截止日期前已记录的合并。"""
        current = athlete_id
        seen = {current}
        while True:
            sql = "SELECT survivor_athlete_id AS s FROM athlete_merges WHERE merged_athlete_id=?"
            params = [current]
            if cutoff is not None:
                sql += " AND recorded_at<=?"
                params.append(cutoff)
            row = self.store.query_one(sql, params)
            if row is None or row["s"] in seen:
                return current
            seen.add(row["s"])
            current = row["s"]

    def _lineage_component(self, event_id: str, cutoff: str | None = None) -> set:
        sql = "SELECT from_event_id AS a, to_event_id AS b FROM event_lineage_links"
        params = []
        if cutoff is not None:
            sql += " WHERE recorded_at<=?"
            params.append(cutoff)
        adjacency: dict[str, set] = {}
        for row in self.store.query(sql, params):
            adjacency.setdefault(row["a"], set()).add(row["b"])
            adjacency.setdefault(row["b"], set()).add(row["a"])
        seen = {event_id}
        stack = [event_id]
        while stack:
            current = stack.pop()
            for nxt in adjacency.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def _lineage_root(self, event_id: str, cutoff: str | None = None) -> str:
        return min(self._lineage_component(event_id, cutoff))

    def _lineage_members(self, event_id: str, cutoff: str | None = None) -> list[str]:
        return sorted(self._lineage_component(event_id, cutoff))

    # ---------- 内部：展示与聚合 ----------
    def _aggregate(self, ordered: list, cutoff: str | None) -> dict:
        per_athlete: Counter = Counter()
        per_event: Counter = Counter()
        per_edition: Counter = Counter()
        holders = {}
        for slot, leaf in ordered:
            holders[slot] = leaf.holder_key
            per_edition[slot[0]] += 1
            per_event[self._lineage_root(slot[1], cutoff)] += 1
            if leaf.holder_kind == "athlete":
                per_athlete[self._resolve_athlete(leaf.athlete_id, cutoff)] += 1
            else:
                for _, athlete_id in self._lineup_members(leaf.lineup_id):
                    per_athlete[self._resolve_athlete(athlete_id, cutoff)] += 1
        return {"athletes": per_athlete, "events": per_event,
                "editions": per_edition, "holders": holders}

    def _counter_delta(self, before: Counter, after: Counter, describe) -> list[dict]:
        deltas = []
        for key in sorted(set(before) | set(after)):
            earlier, later = before.get(key, 0), after.get(key, 0)
            if earlier != later:
                entry = describe(key)
                entry.update({"before": earlier, "after": later, "delta": later - earlier})
                deltas.append(entry)
        return deltas

    def _slot_summary(self, slot: tuple, leaf: ResultVersion, cutoff: str | None) -> dict:
        return {"edition_id": slot[0], "event_id": slot[1], "medal": slot[2],
                "result_id": leaf.result_id, "finalized_on": leaf.finalized_on,
                "holder": self._describe_holder(leaf, cutoff)}

    def _describe_result(self, result: ResultVersion, cutoff: str | None) -> dict:
        return {
            "result_id": result.result_id,
            "edition_id": result.edition_id,
            "event_id": result.event_id,
            "medal": result.medal,
            "delegation": result.delegation,
            "holder": self._describe_holder(result, cutoff),
            "holder_key": result.holder_key,
            "finalized_on": result.finalized_on,
            "supersedes": result.supersedes,
            "evidence_id": result.evidence_id,
            "source": result.source,
            "recorded_at": result.recorded_at,
            "stripped": result.result_id in self._stripped_ids(cutoff),
        }

    def _describe_holder(self, result: ResultVersion, cutoff: str | None) -> dict:
        if result.holder_kind == "athlete":
            resolved = self._resolve_athlete(result.athlete_id, cutoff)
            return {
                "kind": "athlete",
                "athlete_id": resolved,
                "name": self._athlete_name(resolved),
                "recorded_athlete_id": result.athlete_id,
                "recorded_name": self._athlete_name(result.athlete_id),
            }
        members = []
        for leg, athlete_id in self._lineup_members(result.lineup_id):
            resolved = self._resolve_athlete(athlete_id, cutoff)
            members.append({"leg": leg, "athlete_id": resolved,
                            "name": self._athlete_name(resolved),
                            "recorded_athlete_id": athlete_id})
        return {"kind": "relay", "lineup_id": result.lineup_id, "members": members}

    # ---------- 内部：杂项 ----------
    def _require_row(self, sql: str, params: tuple, code: str, message: str) -> dict:
        row = self.store.query_one(sql, params)
        if row is None:
            raise LedgerError(code, message)
        return row

    def _require_sequence(self, sequence_id: str) -> dict:
        return self._require_row(
            "SELECT * FROM sequences WHERE sequence_id=?", (sequence_id,),
            "sequence_not_found", f"统计序列不存在：{sequence_id}")

    def _sequence_scope(self, sequence_id: str) -> tuple[set, set]:
        editions = {r["edition_id"] for r in self.store.query(
            "SELECT edition_id FROM sequence_editions WHERE sequence_id=?", (sequence_id,))}
        events = {r["event_id"] for r in self.store.query(
            "SELECT event_id FROM sequence_events WHERE sequence_id=?", (sequence_id,))}
        return editions, events

    def _get_result(self, result_id: str) -> ResultVersion | None:
        row = self.store.query_one(
            "SELECT * FROM results WHERE result_id=?", (result_id,))
        return ResultVersion.from_row(row) if row else None

    def _require_result(self, result_id: str) -> ResultVersion:
        result = self._get_result(result_id)
        if result is None:
            raise LedgerError("result_not_found", f"结果不存在：{result_id}")
        return result

    def _lineup_members(self, lineup_id: str) -> list[tuple[int, str]]:
        return [(r["leg"], r["athlete_id"]) for r in self.store.query(
            "SELECT leg,athlete_id FROM relay_members WHERE lineup_id=? ORDER BY leg",
            (lineup_id,))]

    def _athlete_name(self, athlete_id: str) -> str:
        row = self.store.query_one(
            "SELECT name FROM athletes WHERE athlete_id=?", (athlete_id,))
        return row["name"] if row else athlete_id

    def _event_name(self, event_id: str) -> str:
        row = self.store.query_one(
            "SELECT name FROM events WHERE event_id=?", (event_id,))
        return row["name"] if row else event_id
