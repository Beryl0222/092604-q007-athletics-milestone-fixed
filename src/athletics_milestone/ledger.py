"""里程碑认定账：导入、冲突隔离、锁定发号、追加裁决与双时间面查询。

设计要点：
- 赛事届次 + 项目规则时代决定统计序列；项目身份由（项目代码、规则时代、
  距离/性别/赛制指纹）确定，距离或名称变化不自动视作同一项目。
- 运动员同名默认不同人，合并必须出示证据；接力按棒次留阵容。
- 公开编号只能由锁定批次原子分配；任何一枚奖牌只能占一个编号。
- 赛后裁决只追加替代关系（vacate/reassign/reinstate），不覆盖当晚认定。
"""
from __future__ import annotations

import json

from .clock import Clock
from .store import Store


class LedgerError(ValueError):
    """账本规则被违反（资料缺失、证据不足、状态不允许等）。"""


def _winner_key(payload: dict) -> str:
    if payload.get("lineup"):
        members = "|".join(sorted(str(a) for a in payload["lineup"]))
        return f"R:{members}"
    return f"A:{payload['athlete_id']}"


class MilestoneLedger:
    def __init__(self, store: Store | None = None, clock: Clock | None = None) -> None:
        self.store = store or Store()
        self.clock = clock or Clock()

    # ================= 基础资料 =================
    def register_edition(self, edition_id, name, year, games_no=None, opened_at=None, closed_at=None):
        self.store.put_edition(edition_id, name, year, games_no, opened_at, closed_at)
        return {"edition_id": edition_id}

    def register_era(self, era_id, discipline, name, effective_from, effective_to=None):
        self.store.put_era(era_id, discipline, name, effective_from, effective_to)
        return {"era_id": era_id}

    def register_event(self, event_id, code, name, discipline, spec, era_id, note=None):
        if not self._era_exists(era_id):
            raise LedgerError(f"规则时代不存在：{era_id}")
        self.store.put_event(event_id, code, name, discipline, spec, era_id, note)
        return {"event_id": event_id}

    def _era_exists(self, era_id):
        row = self.store.connection.execute(
            "SELECT 1 FROM rule_eras WHERE era_id=?", (era_id,)).fetchone()
        return row is not None

    def link_event_lineage(self, old_event_id, new_event_id, note=None):
        """项目沿革只作溯源线索，绝不合并统计身份。"""
        if not self.store.get_event(old_event_id):
            raise LedgerError(f"项目不存在：{old_event_id}")
        if not self.store.get_event(new_event_id):
            raise LedgerError(f"项目不存在：{new_event_id}")
        if old_event_id == new_event_id:
            raise LedgerError("沿革两端不能是同一项目身份")
        self.store.link_events(old_event_id, new_event_id, note)
        return {"old_event_id": old_event_id, "new_event_id": new_event_id, "same_identity": False}

    def register_athlete(self, athlete_id, display_name, gender=None, birth_date=None):
        self.store.put_athlete(athlete_id, display_name, gender, birth_date)
        return {"athlete_id": athlete_id}

    def register_source(self, source_id, **fields):
        self.store.put_source(source_id, **fields)
        return {"source_id": source_id}

    def register_evidence(self, evidence_id, source_id=None, locator=None, excerpt=None):
        if source_id is not None and not self.store.connection.execute(
                "SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
            raise LedgerError(f"证据来源不存在：{source_id}")
        self.store.put_evidence(evidence_id, source_id, locator, excerpt)
        return {"evidence_id": evidence_id}

    def merge_athletes(self, from_id, into_id, evidence_id):
        """同名（或任何）身份合并：证据不存在一律拒绝。"""
        if from_id == into_id:
            raise LedgerError("不能把运动员合并进自身")
        source = self.store.get_athlete(from_id)
        target = self.store.get_athlete(into_id)
        if not source or not target:
            raise LedgerError("运动员身份不存在，无法合并")
        if not evidence_id or not self.store.evidence_exists(evidence_id):
            raise LedgerError("合并运动员身份必须提供已登记证据")
        if self.store.canonical_athlete(from_id) == self.store.canonical_athlete(into_id):
            return {"from": from_id, "into": into_id, "already": True}
        self.store.merge_athlete(from_id, into_id, self.clock.now(), evidence_id)
        return {"from": from_id, "into": into_id, "evidence_id": evidence_id}

    # ================= 导入批次 =================
    def create_batch(self, batch_id, note=None):
        if self.store.get_batch(batch_id):
            raise LedgerError(f"批次已存在：{batch_id}")
        self.store.create_batch(batch_id, self.clock.now(), note)
        return {"batch_id": batch_id, "state": "open"}

    def stage_item(self, batch_id, item: dict):
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise LedgerError(f"批次不存在：{batch_id}")
        if batch["state"] not in ("open",):
            raise LedgerError(f"批次已 {batch['state']}，不能再接收条目")
        result_id = str(item["result_id"])
        edition_id = str(item["edition_id"])
        event_id = str(item["event_id"])
        if not self.store.connection.execute(
                "SELECT 1 FROM editions WHERE edition_id=?", (edition_id,)).fetchone():
            raise LedgerError(f"赛事届次不存在：{edition_id}")
        event = self.store.get_event(event_id)
        if not event:
            raise LedgerError(f"项目不存在：{event_id}")
        payload = dict(item.get("payload") or item)
        payload.setdefault("result_date", item.get("result_date"))
        payload["result_date"] = str(payload["result_date"])
        is_relay = bool(payload.get("lineup"))
        if is_relay:
            missing = [a for a in payload["lineup"] if not self.store.get_athlete(a)]
            if missing:
                raise LedgerError(f"接力阵容含未登记运动员：{missing}")
            payload.setdefault("team_code", item.get("team_code"))
        else:
            athlete_id = str(payload["athlete_id"])
            if not self.store.get_athlete(athlete_id):
                raise LedgerError(f"运动员不存在：{athlete_id}")
            payload["athlete_id"] = athlete_id
        if not payload.get("evidence_id") or not self.store.evidence_exists(payload["evidence_id"]):
            raise LedgerError("每项认定都必须附带已登记证据")

        # 同一 result_id 重复导入 = 幂等；已占用过编号则直接回号。
        existing_item = self.store.item_by_result(result_id)
        if existing_item:
            alloc = self.store.allocation_by_item(existing_item["item_id"])
            return {"result_id": result_id, "deduplicated": True,
                    "milestone_no": alloc["milestone_no"] if alloc else None}

        seq = item.get("seq")
        if seq is None:
            row = self.store.connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM batch_items WHERE batch_id=?",
                (batch_id,)).fetchone()
            seq = row["s"]
        winner_key = _winner_key(payload)
        staged = {"item_id": f"{batch_id}:{result_id}", "batch_id": batch_id, "seq": seq,
                  "result_id": result_id, "edition_id": edition_id, "event_id": event_id,
                  "winner_key": winner_key, "payload": payload, "state": "staged"}
        self.store.add_item(staged)

        # 矛盾结果：同一届次同一项目（严格项目身份）只能有一枚当晚金牌。
        conflict = self._detect_gold_conflict(edition_id, event_id, winner_key, batch_id, result_id)
        if conflict:
            key = f"{edition_id}:{event_id}:{result_id}"
            self.store.mark_item(staged["item_id"], "conflict", conflict["reason"])
            self.store.enqueue_conflict(key, batch_id, staged["item_id"],
                                        conflict["reason"], conflict.get("other_ref"))
            return {"result_id": result_id, "state": "conflict", "reason": conflict["reason"]}
        return {"result_id": result_id, "state": "staged"}

    def _detect_gold_conflict(self, edition_id, event_id, winner_key, batch_id, result_id):
        committed = self.store.committed_gold(edition_id, event_id)
        if committed:
            if committed["result_id"] != result_id:
                return {"reason": "该届该项目已存在正式金牌结果，结果版本矛盾",
                        "other_ref": committed["result_id"]}
        for other in self.store.pending_gold_items(edition_id, event_id, exclude_batch=batch_id):
            if other["state"] == "conflict":
                continue
            if other["result_id"] == result_id:
                continue
            if other["winner_key"] == winner_key:
                return {"reason": "同一金牌胜者存在两个结果版本", "other_ref": other["result_id"]}
            return {"reason": "并行来源对该届该项目金牌胜者认定不一致",
                    "other_ref": other["result_id"]}
        return None

    def open_conflicts(self):
        return self.store.list_open_conflicts()

    def resolve_conflict(self, item_id, action, note=""):
        item = self.store.get_item(item_id)
        if not item or item["state"] != "conflict":
            raise LedgerError("没有待处置的隔离条目")
        if action == "accept":
            self.store.mark_item(item_id, "staged")
        elif action == "discard":
            self.store.mark_item(item_id, "discarded", note)
        else:
            raise LedgerError("处置方式只能是 accept 或 discard")
        self.store.resolve_conflicts_for(item_id, self.clock.now(), f"{action}:{note}")
        return {"item_id": item_id, "action": action}

    # ================= 锁定与原子发号 =================
    def batch_status(self, batch_id):
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise LedgerError(f"批次不存在：{batch_id}")
        items = self.store.items_of_batch(batch_id)
        allocs = {a["item_id"]: a["milestone_no"] for a in self.store.allocations_of_batch(batch_id)}
        return {"batch_id": batch_id, "state": batch["state"],
                "items": [{"item_id": i["item_id"], "result_id": i["result_id"],
                           "state": i["state"], "milestone_no": allocs.get(i["item_id"]),
                           "reason": i["conflict_reason"]} for i in items]}

    def lock_batch(self, batch_id):
        """把编号一次性原子占住。中断后重入直接复用占用表。"""
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise LedgerError(f"批次不存在：{batch_id}")
        existing = self.store.allocations_of_batch(batch_id)
        if existing:
            return {"batch_id": batch_id, "state": "locked", "recovered": True,
                    "allocations": existing}
        if batch["state"] != "open":
            raise LedgerError(f"批次状态为 {batch['state']}，无法锁定")
        staged = [i for i in self.store.items_of_batch(batch_id) if i["state"] == "staged"]
        staged.sort(key=lambda i: (i["payload"]["result_date"], i["seq"]))
        # MAX 与占用插入在同一个 IMMEDIATE 事务内：并行批次必须串行经过
        # 这个临界点，任何两枚奖牌都不可能读到同一个起点编号。
        def _allocate(conn):
            row = conn.execute("SELECT MAX(milestone_no) AS m FROM allocations").fetchone()
            next_no = (row["m"] or 0) + 1
            allocations = []
            for item in staged:
                allocations.append((batch_id, item["item_id"], item["result_id"], next_no))
                next_no += 1
            conn.execute("UPDATE batches SET state='locked', locked_at=? WHERE batch_id=?",
                         (self.clock.now(), batch_id))
            conn.executemany(
                "INSERT INTO allocations(batch_id,item_id,result_id,milestone_no) VALUES(?,?,?,?)",
                allocations)
            return [{"batch_id": b, "item_id": i, "result_id": r, "milestone_no": n}
                    for b, i, r, n in allocations]
        allocations = self._with_write_retry(_allocate)
        return {"batch_id": batch_id, "state": "locked", "allocations": allocations}

    def _with_write_retry(self, fn, attempts=20):
        import sqlite3
        import time
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with self.store.transaction() as conn:
                    return fn(conn)
            except sqlite3.OperationalError as exc:  # database is locked / busy
                last = exc
                time.sleep(0.05 * (attempt + 1))
        raise LedgerError(f"账本写冲突，重试后仍失败：{last}")

    def commit_batch(self, batch_id):
        """提交认定。若此前在锁定后中断，则从占用表恢复，编号不变。"""
        batch = self.store.get_batch(batch_id)
        if not batch:
            raise LedgerError(f"批次不存在：{batch_id}")
        if batch["state"] == "committed":
            return self.batch_status(batch_id)
        locked = self.lock_batch(batch_id)
        allocations = self.store.allocations_of_batch(batch_id)
        committed = []

        def _commit(conn):
            committed.clear()  # 重试时丢弃上次未提交事务内的内存累积
            conn.execute("UPDATE batches SET state='committing' WHERE batch_id=?", (batch_id,))
            for alloc in allocations:
                item = self.store.get_item(alloc["item_id"])
                payload = item["payload"]
                is_relay = bool(payload.get("lineup"))
                conn.execute(
                    "INSERT OR IGNORE INTO results(result_id,edition_id,event_id,place,athlete_id,"
                    "team_code,performance,performance_unit,result_date,source_id,status,committed_batch)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item["result_id"], item["edition_id"], item["event_id"], 1,
                     None if is_relay else payload["athlete_id"],
                     payload.get("team_code"), payload.get("performance"),
                     payload.get("performance_unit"), payload["result_date"],
                     payload.get("source_id"), "official", batch_id))
                if is_relay:
                    conn.execute("DELETE FROM relay_lineups WHERE result_id=?", (item["result_id"],))
                    conn.executemany(
                        "INSERT INTO relay_lineups(result_id,leg,athlete_id) VALUES(?,?,?)",
                        [(item["result_id"], leg + 1, aid)
                         for leg, aid in enumerate(payload["lineup"])])
                basis = {"evidence_id": payload.get("evidence_id"),
                         "source_id": payload.get("source_id"),
                         "published_note": payload.get("published_note", "赛事当晚按发布顺序编号"),
                         "winner_key": item["winner_key"], "batch_id": batch_id}
                conn.execute(
                    "INSERT OR IGNORE INTO assertions(milestone_no,result_id,edition_id,event_id,"
                    "athlete_id,team_code,valid_from,batch_id,basis_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (alloc["milestone_no"], item["result_id"], item["edition_id"], item["event_id"],
                     None if is_relay else payload["athlete_id"], payload.get("team_code"),
                     payload["result_date"], batch_id,
                     json.dumps(basis, ensure_ascii=False)))
                conn.execute(
                    "UPDATE batch_items SET state='done', milestone_no=? WHERE item_id=?",
                    (alloc["milestone_no"], alloc["item_id"]))
                committed.append({"item_id": alloc["item_id"], "result_id": item["result_id"],
                                  "milestone_no": alloc["milestone_no"]})
            conn.execute("UPDATE batches SET state='committed' WHERE batch_id=?", (batch_id,))

        # 应用阶段单事务：任何失败整体回滚，占用号保留，下次照旧恢复。
        self._with_write_retry(_commit)
        return {"batch_id": batch_id, "state": "committed", "recovered": locked.get("recovered", False),
                "committed": committed,
                "quarantined": [i["item_id"] for i in self.store.items_of_batch(batch_id)
                                if i["state"] in ("conflict", "discarded")]}

    def recover_batch(self, batch_id):
        """中断恢复入口：锁定已落盘则沿用原编号继续提交。"""
        return self.commit_batch(batch_id)

    # ================= 非里程碑结果（升牌者等） =================
    def register_supplemental_result(self, result: dict):
        """登记不占里程碑编号的结果（如原亚军、后来递补金牌），须附证据。"""
        if not result.get("evidence_id") or not self.store.evidence_exists(result["evidence_id"]):
            raise LedgerError("补充结果必须附带已登记证据")
        if self.store.result_exists(result["result_id"]):
            return {"result_id": result["result_id"], "existed": True}
        if not self.store.get_event(result["event_id"]):
            raise LedgerError(f"项目不存在：{result['event_id']}")
        self.store.insert_result({"place": 2, "status": "official", **result})
        return {"result_id": result["result_id"]}

    # ================= 赛后裁决（只追加） =================
    def adjudicate(self, adj_id, kind, milestone_no, adjudicated_at=None, reason="",
                   evidence_id=None, to_result=None, to_athlete_id=None, to_team_code=None):
        if not self.store.get_assertion(milestone_no):
            raise LedgerError(f"不存在第 {milestone_no} 号认定，无法裁决")
        if kind not in ("vacate", "reassign", "reinstate"):
            raise LedgerError("裁决类型只能是 vacate/reassign/reinstate")
        if not evidence_id or not self.store.evidence_exists(evidence_id):
            raise LedgerError("赛后裁决必须附带已登记证据")
        if self.store.connection.execute(
                "SELECT 1 FROM adjudications WHERE adj_id=?", (adj_id,)).fetchone():
            raise LedgerError(f"裁决编号重复：{adj_id}")
        at = adjudicated_at or self.clock.now()
        assertion = self.store.get_assertion(milestone_no)
        effects = []
        target_result_id = assertion["result_id"]

        if kind == "vacate":
            effects.append({"milestone_no": milestone_no, "effect": "vacate",
                            "result_id": target_result_id, "athlete_id": None, "team_code": None})
        elif kind == "reassign":
            new_result_id = self._ensure_replacement_result(to_result, assertion, evidence_id, to_team_code)
            new_result = self.store.get_result(new_result_id)
            new_athlete = to_athlete_id or new_result.get("athlete_id")
            new_team = to_team_code or new_result.get("team_code")
            # 先追加“剥夺”，再追加“替代”：当年记录保持不变。
            effects.append({"milestone_no": milestone_no, "effect": "vacate",
                            "result_id": target_result_id, "athlete_id": None, "team_code": None})
            effects.append({"milestone_no": milestone_no, "effect": "reassign",
                            "result_id": new_result_id, "athlete_id": new_athlete,
                            "team_code": new_team})
        else:  # reinstate
            effects.append({"milestone_no": milestone_no, "effect": "reinstate",
                            "result_id": target_result_id,
                            "athlete_id": assertion["athlete_id"],
                            "team_code": assertion["team_code"]})

        self.store.insert_adjudication(
            {"adj_id": adj_id, "adjudicated_at": at, "kind": kind,
             "milestone_no": milestone_no, "target_result_id": target_result_id,
             "to_result_id": None if kind == "vacate" else (
                 effects[-1]["result_id"] if kind == "reassign" else target_result_id),
             "to_athlete_id": to_athlete_id, "to_team_code": to_team_code,
             "reason": reason, "evidence_id": evidence_id, "effects": effects})
        self.store.connection.execute(
            "UPDATE results SET status='vacated' WHERE result_id=?", (target_result_id,))
        if kind == "reassign":
            self.store.connection.execute(
                "UPDATE results SET status='reallocated' WHERE result_id=?",
                (effects[-1]["result_id"],))
            self.store.connection.commit()
        return {"adj_id": adj_id, "kind": kind, "milestone_no": milestone_no,
                "adjudicated_at": at, "effects": effects}

    def _ensure_replacement_result(self, to_result, assertion, evidence_id, to_team_code):
        if isinstance(to_result, str):
            if not self.store.result_exists(to_result):
                raise LedgerError(f"递补结果不存在：{to_result}")
            return to_result
        if isinstance(to_result, dict):
            result = {"place": 2, "evidence_id": evidence_id, **to_result}
            result.setdefault("edition_id", assertion["edition_id"])
            result.setdefault("event_id", assertion["event_id"])
            if not self.store.get_event(result["event_id"]):
                raise LedgerError(f"项目不存在：{result['event_id']}")
            if result.get("athlete_id") and not self.store.get_athlete(result["athlete_id"]):
                raise LedgerError(f"运动员不存在：{result['athlete_id']}")
            for aid in result.get("lineup", []):
                if not self.store.get_athlete(aid):
                    raise LedgerError(f"接力阵容含未登记运动员：{aid}")
            self.store.insert_result({"status": "official", **result})
            return result["result_id"]
        raise LedgerError("reassign 必须给出 to_result（已有 result_id 或结果内容）")

    def list_adjudications(self, milestone_no=None):
        sql = "SELECT * FROM adjudications"
        args: list = []
        if milestone_no is not None:
            sql += " WHERE milestone_no=?"
            args.append(milestone_no)
        sql += " ORDER BY adjudicated_at, adj_id"
        return [dict(r) for r in self.store.connection.execute(sql, args).fetchall()]

    # ================= 双时间面查询 =================
    def _states_as_of(self, as_of: str | None):
        as_of = as_of or self.clock.now()
        states: dict[int, dict] = {}
        for a in self.store.assertions_as_of(as_of):
            states[a["milestone_no"]] = {
                "milestone_no": a["milestone_no"], "status": "official",
                "result_id": a["result_id"], "edition_id": a["edition_id"],
                "event_id": a["event_id"], "athlete_id": a["athlete_id"],
                "team_code": a["team_code"], "valid_from": a["valid_from"]}
        for e in self.store.effects_as_of(as_of):
            st = states.get(e["milestone_no"])
            if st is None:
                continue
            st["status"] = {"vacate": "vacated", "reassign": "reassigned",
                            "reinstate": "official"}[e["effect"]]
            st["result_id"] = e["result_id"]
            st["athlete_id"] = e["athlete_id"]
            result = self.store.get_result(e["result_id"]) if e["result_id"] else None
            st["event_id"] = result["event_id"] if result else st["event_id"]
            st["edition_id"] = result["edition_id"] if result else st["edition_id"]
            st["team_code"] = e["team_code"]
        return states

    def _describe_result(self, result_id, athlete_id, team_code):
        result = self.store.get_result(result_id) if result_id else None
        lineup = []
        if result:
            lineup = [self._describe_athlete(aid) for aid in self.store.lineup_of(result_id)]
        athlete = self._describe_athlete(athlete_id) if athlete_id else None
        event = self.store.get_event(result["event_id"]) if result else None
        edition = None
        if result:
            row = self.store.connection.execute(
                "SELECT * FROM editions WHERE edition_id=?", (result["edition_id"],)).fetchone()
            edition = dict(row) if row else None
        return {"result_id": result_id, "athlete": athlete, "lineup": lineup,
                "team_code": team_code, "performance": result["performance"] if result else None,
                "result_date": result["result_date"] if result else None,
                "event": event, "edition": edition}

    def _describe_athlete(self, athlete_id):
        canonical_id = self.store.canonical_athlete(athlete_id)
        row = self.store.get_athlete(canonical_id)
        original = self.store.get_athlete(athlete_id)
        return {"athlete_id": canonical_id, "display_name": row["display_name"] if row else None,
                "recorded_as": None if canonical_id == athlete_id or not original
                else {"athlete_id": athlete_id, "display_name": original["display_name"],
                      "merged_evidence": original["merged_evidence"]}}

    def milestone(self, milestone_no: int, as_of: str | None = None):
        """第 N 枚：同时给出当晚发布答案与指定日期（默认今天）的有效答案。"""
        assertion = self.store.get_assertion(milestone_no)
        if not assertion:
            raise LedgerError(f"不存在第 {milestone_no} 号认定")
        states = self._states_as_of(as_of)
        state = states.get(milestone_no)
        published = self._describe_result(
            assertion["result_id"], assertion["athlete_id"], assertion["team_code"])
        ruling_rows = self.list_adjudications(milestone_no)
        rulings = []
        for r in ruling_rows:
            if as_of is None or r["adjudicated_at"] <= as_of:
                rulings.append({"adj_id": r["adj_id"], "kind": r["kind"],
                                "adjudicated_at": r["adjudicated_at"], "reason": r["reason"],
                                "evidence_id": r["evidence_id"]})
        current = None
        if state:
            if state["status"] == "vacated":
                current = {"status": "vacated", "holder": None,
                           "note": "该编号奖牌目前处于空缺，尚未有递补者"}
            else:
                described = self._describe_result(state["result_id"], state["athlete_id"],
                                                  state["team_code"])
                current = {"status": state["status"], "holder": described}
        published_note = assertion["basis"].get("published_note")
        return {
            "milestone_no": milestone_no,
            "published": {"at": assertion["valid_from"], "winner": published,
                          "basis": assertion["basis"],
                          "note": published_note or "当晚按发布顺序原子编号，编号不可更改"},
            "current": current,
            "rulings": rulings,
            "answers_coexist": bool(rulings),
            "explanation": self._explain(milestone_no, assertion, current, rulings),
        }

    def _explain(self, no, assertion, current, rulings):
        pub = self._describe_result(assertion["result_id"], assertion["athlete_id"],
                                    assertion["team_code"])
        pub_name = self._winner_label(pub)
        if not rulings or current is None:
            return (f"第 {no} 枚奖牌自 {assertion['valid_from']} 当晚发布以来未受裁决影响，"
                    f"当晚答案与今天答案一致：{pub_name}。")
        if current["status"] == "vacated":
            return (f"第 {no} 枚奖牌当晚编号给 {pub_name}；赛后经 {len(rulings)} 项裁决被剥夺，"
                    "当晚认定仍是历史事实（编号不回收），今天该编号空缺。")
        cur_name = self._winner_label(current["holder"])
        kind_text = "递补改判" if current["status"] == "reassigned" else "裁决后恢复"
        return (f"第 {no} 枚奖牌当晚编号给 {pub_name}（编号当时候者唯一、原子分配，不可更改）；"
                f"赛后{kind_text}，今天有效的持有者是 {cur_name}。"
                "两个答案各自属于其生效日期，凭追加的裁决关系同时成立。")

    @staticmethod
    def _winner_label(described):
        if described["lineup"]:
            names = "/".join(a["display_name"] for a in described["lineup"])
            return f"{described['team_code']} 接力队（{names}）"
        if described["athlete"]:
            team = f"（{described['team_code']}）" if described["team_code"] else ""
            return f"{described['athlete']['display_name']}{team}"
        return described["team_code"] or "未知"

    def snapshot(self, as_of: str):
        """指定历史日期的认定快照：当晚序列在该日的全部有效状态。"""
        states = self._states_as_of(as_of)
        medals = []
        for no in sorted(states):
            st = states[no]
            if st["status"] == "vacated":
                medals.append({"milestone_no": no, "status": "vacated", "holder": None})
            else:
                medals.append({"milestone_no": no, "status": st["status"],
                               "holder": self._describe_result(
                                   st["result_id"], st["athlete_id"], st["team_code"])})
        return {"as_of": as_of, "count": len(medals), "medals": medals}

    # ================= 统计与裁决差异 =================
    def _held_athletes(self, state):
        """某编号在某状态下计入个人统计的运动员（接力含全部棒次，身份取当前 canonical）。"""
        if state["status"] == "vacated" or not state["result_id"]:
            return []
        lineup = self.store.lineup_of(state["result_id"])
        if lineup:
            return [self.store.canonical_athlete(a) for a in lineup]
        if state["athlete_id"]:
            return [self.store.canonical_athlete(state["athlete_id"])]
        return []

    def person_tally(self, athlete_id, as_of=None):
        target = self.store.canonical_athlete(athlete_id)
        states = self._states_as_of(as_of)
        medals = []
        for no in sorted(states):
            if target in self._held_athletes(states[no]):
                medals.append(no)
        name = self.store.get_athlete(target)
        return {"athlete_id": target,
                "display_name": name["display_name"] if name else None,
                "as_of": as_of or self.clock.now(), "gold_count": len(medals),
                "milestone_numbers": medals}

    def team_tally(self, team_code, as_of=None):
        states = self._states_as_of(as_of)
        medals = [no for no in sorted(states)
                  if states[no]["status"] != "vacated" and states[no]["team_code"] == team_code]
        return {"team_code": team_code, "as_of": as_of or self.clock.now(),
                "gold_count": len(medals), "milestone_numbers": medals}

    def ruling_impacts(self, since: str | None = None, until: str | None = None):
        """列出窗口内裁决给个人、项目、代表团统计带来的差异。"""
        before = self._states_as_of(since) if since else {}
        after = self._states_as_of(until)
        person_rows: list[dict] = []
        event_rows: list[dict] = []
        team_delta: dict[str, int] = {}
        adj_map = {r["adj_id"]: r for r in self.store.connection.execute(
            "SELECT * FROM adjudications").fetchall()}
        for r in self.store.connection.execute(
                "SELECT DISTINCT adj_id, milestone_no FROM adjudication_effects ORDER BY adj_id"):
            adj = adj_map[r["adj_id"]]
            if since and adj["adjudicated_at"] <= since:
                continue
            if until and adj["adjudicated_at"] > until:
                continue
            no = r["milestone_no"]
            old = before.get(no)
            new = after.get(no)
            old_athletes = set(self._held_athletes(old)) if old else set()
            new_athletes = set(self._held_athletes(new)) if new else set()
            for aid in sorted(old_athletes - new_athletes):
                person_rows.append(self._impact_person(aid, no, -1, adj))
            for aid in sorted(new_athletes - old_athletes):
                person_rows.append(self._impact_person(aid, no, +1, adj))
            old_team = old["team_code"] if old and old["status"] != "vacated" else None
            new_team = new["team_code"] if new and new["status"] != "vacated" else None
            if old_team != new_team:
                if old_team:
                    team_delta[old_team] = team_delta.get(old_team, 0) - 1
                if new_team:
                    team_delta[new_team] = team_delta.get(new_team, 0) + 1
            old_result = old["result_id"] if old else None
            new_result = new["result_id"] if new and new["status"] != "vacated" else None
            if old_result != new_result or (old and new and old["status"] != new["status"]):
                event_rows.append({
                    "milestone_no": no, "adj_id": adj["adj_id"],
                    "adjudicated_at": adj["adjudicated_at"],
                    "event_id": (new or old)["event_id"],
                    "edition_id": (new or old)["edition_id"],
                    "from_result_id": old_result, "to_result_id": new_result,
                    "status_after": new["status"] if new else None})
        return {
            "since": since, "until": until,
            "person_changes": person_rows,
            "event_changes": event_rows,
            "team_tally_delta": [{"team_code": t, "delta": d}
                                 for t, d in sorted(team_delta.items()) if d],
        }

    def _impact_person(self, aid, no, delta, adj):
        athlete = self.store.get_athlete(aid)
        return {"athlete_id": aid, "display_name": athlete["display_name"] if athlete else None,
                "milestone_no": no, "delta": delta, "adj_id": adj["adj_id"],
                "adjudicated_at": adj["adjudicated_at"], "reason": adj["reason"]}

    def event_lineage(self, event_id):
        """项目沿革视图：明确标注身份不连续，统计不得跨身份合并。"""
        event = self.store.get_event(event_id)
        if not event:
            raise LedgerError(f"项目不存在：{event_id}")
        chain = [event]
        current = event
        while current.get("superseded_by"):
            nxt = self.store.get_event(current["superseded_by"])
            if not nxt:
                break
            chain.append(nxt)
            current = nxt
        return {"events": chain, "same_identity": False,
                "note": "距离、性别、赛制或规则时代不同即不同项目身份；沿革仅用于溯源"}
