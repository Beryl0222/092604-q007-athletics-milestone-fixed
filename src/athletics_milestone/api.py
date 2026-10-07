"""处理进程内 JSON 请求。

写动作支持 ``request_key`` 幂等：同一键重放返回首次响应，
键相同但载荷不同会被拒绝，避免并行导入重复发号。
"""
import hashlib
import json

from .ledger import LedgerError
from .service import Service


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    body = json.loads(raw)
    action = body.get("action")

    ledger = current.ledger
    request_key = body.get("request_key")
    if request_key is not None:
        payload_hash = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        cached = current.store.get_receipt(request_key)
        if cached is not None:
            receipt = json.loads(cached)
            if receipt["payload_hash"] != payload_hash:
                raise LedgerError("request_key 已被使用但请求载荷不一致")
            return receipt["response"]

    if action == "health":
        result = current.health()
    elif action == "register":
        result = current.register(str(body["record_id"]), str(body["owner_id"]))
    elif action == "find":
        result = current.find(str(body["record_id"]))

    # ---- 基础资料 ----
    elif action == "register_edition":
        result = ledger.register_edition(
            body["edition_id"], body["name"], int(body["year"]),
            games_no=body.get("games_no"), opened_at=body.get("opened_at"),
            closed_at=body.get("closed_at"))
    elif action == "register_era":
        result = ledger.register_era(
            body["era_id"], body["discipline"], body["name"],
            body["effective_from"], body.get("effective_to"))
    elif action == "register_event":
        result = ledger.register_event(
            body["event_id"], body["code"], body["name"], body["discipline"],
            body["spec"], body["era_id"], body.get("note"))
    elif action == "link_event_lineage":
        result = ledger.link_event_lineage(
            body["old_event_id"], body["new_event_id"], body.get("note"))
    elif action == "register_athlete":
        result = ledger.register_athlete(
            body["athlete_id"], body["display_name"],
            body.get("gender"), body.get("birth_date"))
    elif action == "merge_athletes":
        result = ledger.merge_athletes(
            body["from_id"], body["into_id"], body["evidence_id"])
    elif action == "register_source":
        fields = {k: body[k] for k in
                  ("kind", "title", "publisher", "published_at", "uri") if k in body}
        result = ledger.register_source(body["source_id"], **fields)
    elif action == "register_evidence":
        result = ledger.register_evidence(
            body["evidence_id"], body.get("source_id"),
            body.get("locator"), body.get("excerpt"))

    # ---- 批次导入 ----
    elif action == "create_batch":
        result = ledger.create_batch(body["batch_id"], body.get("note"))
    elif action == "stage_item":
        result = ledger.stage_item(body["batch_id"], body["item"])
    elif action == "open_conflicts":
        result = {"conflicts": ledger.open_conflicts()}
    elif action == "resolve_conflict":
        result = ledger.resolve_conflict(
            body["item_id"], body["resolution"], body.get("note", ""))
    elif action == "batch_status":
        result = ledger.batch_status(body["batch_id"])
    elif action == "lock_batch":
        result = ledger.lock_batch(body["batch_id"])
    elif action == "commit_batch":
        result = ledger.commit_batch(body["batch_id"])
    elif action == "recover_batch":
        result = ledger.recover_batch(body["batch_id"])
    elif action == "register_supplemental_result":
        result = ledger.register_supplemental_result(body["result"])

    # ---- 裁决 ----
    elif action == "adjudicate":
        result = ledger.adjudicate(
            body["adj_id"], body["kind"], int(body["milestone_no"]),
            adjudicated_at=body.get("adjudicated_at"), reason=body.get("reason", ""),
            evidence_id=body["evidence_id"], to_result=body.get("to_result"),
            to_athlete_id=body.get("to_athlete_id"), to_team_code=body.get("to_team_code"))
    elif action == "list_adjudications":
        result = {"adjudications": ledger.list_adjudications(
            int(body["milestone_no"]) if body.get("milestone_no") is not None else None)}

    # ---- 查询 ----
    elif action == "milestone":
        result = ledger.milestone(int(body["milestone_no"]), body.get("as_of"))
    elif action == "snapshot":
        result = ledger.snapshot(body["as_of"])
    elif action == "person_tally":
        result = ledger.person_tally(body["athlete_id"], body.get("as_of"))
    elif action == "team_tally":
        result = ledger.team_tally(body["team_code"], body.get("as_of"))
    elif action == "ruling_impacts":
        result = ledger.ruling_impacts(body.get("since"), body.get("until"))
    elif action == "event_lineage":
        result = ledger.event_lineage(body["event_id"])
    else:
        raise ValueError("不支持的请求动作")

    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if request_key is not None:
        envelope = json.dumps({"payload_hash": payload_hash, "response": text},
                              ensure_ascii=False, sort_keys=True)
        current.store.put_receipt(request_key, payload_hash, envelope)
    return text
