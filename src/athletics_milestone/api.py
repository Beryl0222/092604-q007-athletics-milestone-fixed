"""处理进程内 JSON 请求。"""
import json

from .domain import LedgerError
from .service import Service


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    body = json.loads(raw)
    action = body.get("action")
    try:
        result = _dispatch(action, body, current)
    except LedgerError as exc:
        result = {"ok": False, "code": exc.code, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _dispatch(action: str, body: dict, service: Service):
    if action == "health":
        return service.health()
    if action == "register":
        return service.register(str(body["record_id"]), str(body["owner_id"]))
    if action == "find":
        return service.find(str(body["record_id"]))
    if action == "add_edition":
        return service.add_edition(str(body["edition_id"]), int(body["year"]),
                                   str(body["name"]))
    if action == "add_event":
        return service.add_event(str(body["event_id"]), str(body["name"]),
                                 str(body["sex"]), str(body.get("distance", "")),
                                 str(body.get("era_label", "")))
    if action == "add_athlete":
        return service.add_athlete(str(body["athlete_id"]), str(body["name"]))
    if action == "add_evidence":
        return service.add_evidence(str(body["evidence_id"]), str(body["kind"]),
                                    str(body["title"]), str(body["reference"]))
    if action == "define_sequence":
        return service.define_sequence(str(body["sequence_id"]), str(body["name"]),
                                       str(body["delegation"]), str(body["medal"]),
                                       list(body.get("edition_ids", [])),
                                       list(body.get("event_ids", [])))
    if action == "sequence_add_edition":
        return service.sequence_add_edition(str(body["sequence_id"]),
                                            str(body["edition_id"]))
    if action == "sequence_add_event":
        return service.sequence_add_event(str(body["sequence_id"]),
                                          str(body["event_id"]))
    if action == "import_results":
        return service.import_results(str(body["source"]), str(body["request_key"]),
                                      list(body["results"]))
    if action == "open_batch":
        return service.open_batch(str(body["sequence_id"]), str(body["request_key"]))
    if action == "lock_batch":
        return service.lock_batch(str(body["batch_id"]))
    if action == "commit_batch":
        return service.commit_batch(str(body["batch_id"]))
    if action == "recover_batches":
        return service.recover_batches()
    if action == "record_ruling":
        payload = {key: value for key, value in body.items()
                   if key not in ("action", "kind", "evidence_id", "note")}
        return service.record_ruling(str(body["kind"]), str(body["evidence_id"]),
                                     str(body.get("note", "")), **payload)
    if action == "resolve_quarantine":
        return service.resolve_quarantine(str(body["case_id"]), str(body["decision"]),
                                          str(body["evidence_id"]),
                                          str(body.get("note", "")))
    if action == "list_quarantine":
        return {"cases": service.list_quarantine(bool(body.get("only_open", True)))}
    if action == "merge_athletes":
        return service.merge_athletes(str(body["merged_athlete_id"]),
                                      str(body["survivor_athlete_id"]),
                                      str(body["evidence_id"]))
    if action == "link_event_lineage":
        return service.link_event_lineage(str(body["from_event_id"]),
                                          str(body["to_event_id"]),
                                          str(body["evidence_id"]))
    if action == "milestone":
        return service.milestone(str(body["sequence_id"]), int(body["number"]),
                                 body.get("as_of"))
    if action == "snapshot":
        return service.snapshot(str(body["sequence_id"]), str(body["as_of"]))
    if action == "stats":
        return service.stats(str(body["sequence_id"]), body.get("as_of"))
    if action == "stats_diff":
        return service.stats_diff(str(body["sequence_id"]), str(body["since"]))
    raise ValueError("不支持的请求动作")
