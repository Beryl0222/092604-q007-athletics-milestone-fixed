"""里程碑认定账的行为测试。

覆盖的不变量：
- 公开编号只能由锁定批次原子分配，一枚奖牌不会抢到两个编号；
- 矛盾结果先隔离再处置；批次中断从账本恢复；
- 赛后裁决只追加替代关系，当晚答案与当前答案同时成立；
- 同名运动员凭证据合并，项目更名或距离变化不自动视作同一项目；
- 历史日期快照与后续裁决差异可查。
"""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from athletics_milestone.api import handle
from athletics_milestone.clock import Clock
from athletics_milestone.domain import LedgerError
from athletics_milestone.service import Service
from athletics_milestone.store import Store

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-02-01T00:00:00+00:00"
T2 = "2026-03-01T00:00:00+00:00"
T3 = "2026-04-01T00:00:00+00:00"


class 假时钟(Clock):
    def __init__(self, start=T0):
        self.current = start

    def now(self):
        return self.current

    def 推进到(self, moment):
        self.current = moment


def 建账():
    clock = 假时钟()
    service = Service(Store(), clock)
    service.add_edition("ed-1974", 1974, "德黑兰亚运会")
    service.add_edition("ed-1978", 1978, "曼谷亚运会")
    service.add_event("ev-m100", "男子100米", "M", "100m", "era-a")
    service.add_event("ev-w3000", "女子3000米", "F", "3000m", "era-a")
    service.add_event("ev-w5000", "女子5000米", "F", "5000m", "era-b")
    service.add_event("ev-m4x100", "男子4×100米接力", "M", "4x100m", "era-a")
    for athlete_id, name in [
        ("at-su", "苏某"), ("at-li", "李某"), ("at-zhao", "赵某"),
        ("at-wang", "王某"), ("at-zhang1", "张伟"), ("at-zhang2", "张伟"),
        ("at-r1", "甲"), ("at-r2", "乙"), ("at-r3", "丙"), ("at-r4", "丁"),
        ("at-r5", "戊"),
    ]:
        service.add_athlete(athlete_id, name)
    service.add_evidence("doc-1", "official_report", "官方成绩公报", "OCA-ATH-1")
    service.add_evidence("doc-ruling", "federation_ruling", "联合会裁决书", "CAS-2026-01")
    service.add_evidence("doc-identity", "identity_check", "身份核验材料", "CID-88")
    service.add_evidence("doc-rename", "event_history", "项目沿革说明", "WA-RULE-1996")
    service.define_sequence(
        "seq-chn-gold", "中国田径亚运金牌序列", "CHN", "gold",
        ["ed-1974", "ed-1978"], ["ev-m100", "ev-w3000", "ev-w5000", "ev-m4x100"])
    return service, clock


def 金牌(edition, event, *, athlete=None, members=None, finalized,
         evidence="doc-1", delegation="CHN", result_id=None):
    item = {"edition_id": edition, "event_id": event, "medal": "gold",
            "delegation": delegation, "finalized_on": finalized,
            "evidence_id": evidence}
    if athlete:
        item["athlete_id"] = athlete
    if members is not None:
        item["lineup"] = {"members": [
            {"leg": index + 1, "athlete_id": member}
            for index, member in enumerate(members)]}
    if result_id:
        item["result_id"] = result_id
    return item


def 认定全部(service, key="batch-1"):
    batch = service.open_batch("seq-chn-gold", key)
    service.lock_batch(batch["batch_id"])
    return service.commit_batch(batch["batch_id"])


class 编号与批次测试(unittest.TestCase):
    def test_编号由锁定批次按规范顺序原子分配(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "k1", [金牌(
            "ed-1974", "ev-m100", athlete="at-su", finalized="1974-09-02T10:00:00+00:00")])
        service.import_results("src-a", "k2", [金牌(
            "ed-1974", "ev-w3000", athlete="at-li", finalized="1974-09-03T10:00:00+00:00")])
        service.import_results("src-a", "k3", [金牌(
            "ed-1978", "ev-w5000", athlete="at-zhao", finalized="1978-09-01T10:00:00+00:00")])

        committed = 认定全部(service)
        self.assertEqual([a["number"] for a in committed["assignments"]], [1, 2, 3])
        self.assertEqual(committed["assignments"][0]["event_id"], "ev-m100")
        self.assertEqual(committed["assignments"][1]["event_id"], "ev-w3000")
        self.assertEqual(committed["assignments"][2]["event_id"], "ev-w5000")

        again = service.commit_batch(
            service.open_batch("seq-chn-gold", "batch-1")["batch_id"])
        self.assertTrue(again["replayed"])
        self.assertEqual(len(again["assignments"]), 3)

        clock.推进到(T2)
        service.import_results("src-a", "k4", [金牌(
            "ed-1978", "ev-m100", athlete="at-wang", finalized="1978-09-02T10:00:00+00:00")])
        second = 认定全部(service, "batch-2")
        self.assertEqual([a["number"] for a in second["assignments"]], [4])

    def test_未锁定批次不能分配编号(self):
        service, clock = 建账()
        clock.推进到(T1)
        batch = service.open_batch("seq-chn-gold", "batch-x")
        with self.assertRaises(LedgerError) as ctx:
            service.commit_batch(batch["batch_id"])
        self.assertEqual(ctx.exception.code, "batch_not_locked")

    def test_并行提交不会让一个奖牌抢到两个编号(self):
        service, clock = 建账()
        clock.推进到(T1)
        events = ["ev-m100", "ev-w3000", "ev-w5000", "ev-m4x100"]
        for index, event in enumerate(events):
            item = 金牌("ed-1974", event, athlete=f"at-r{index + 1}",
                        finalized=f"1974-09-0{index + 2}T10:00:00+00:00") \
                if event != "ev-m4x100" else 金牌(
                    "ed-1974", event, members=["at-r1", "at-r2", "at-r3", "at-r4"],
                    finalized="1974-09-05T10:00:00+00:00")
            service.import_results("src-a", f"seed-{event}", [item])
        first = service.open_batch("seq-chn-gold", "batch-a")
        second = service.open_batch("seq-chn-gold", "batch-b")
        service.lock_batch(first["batch_id"])
        service.lock_batch(second["batch_id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                service.commit_batch, [first["batch_id"], second["batch_id"]]))
        assigned = [a for outcome in outcomes for a in outcome["assignments"]]
        rows = service.store.query("SELECT * FROM recognitions")
        self.assertEqual(len(rows), 4)
        self.assertEqual(sorted(r["number"] for r in rows), [1, 2, 3, 4])
        self.assertEqual(len({(r["edition_id"], r["event_id"], r["medal"])
                              for r in rows}), 4)
        self.assertEqual(len(assigned), 4)

    def test_并行导入矛盾结果只有一个进入有效链(self):
        service, clock = 建账()
        clock.推进到(T1)

        def 导入(source, key, athlete):
            return service.import_results(source, key, [金牌(
                "ed-1974", "ev-m100", athlete=athlete,
                finalized="1974-09-02T10:00:00+00:00")])

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda args: 导入(*args),
                [("src-a", "k-a", "at-su"), ("src-b", "k-b", "at-li")]))
        statuses = sorted(o["outcomes"][0]["status"] for o in outcomes)
        self.assertEqual(statuses, ["inserted", "quarantined"])
        cases = service.list_quarantine()
        self.assertEqual(len(cases), 1)
        stats = service.stats("seq-chn-gold")
        self.assertEqual(stats["total_medals"], 1)

    def test_批次中断后从账本恢复(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "k1", [金牌(
            "ed-1974", "ev-m100", athlete="at-su", finalized="1974-09-02T10:00:00+00:00")])
        service.import_results("src-a", "k2", [金牌(
            "ed-1974", "ev-w3000", athlete="at-li", finalized="1974-09-03T10:00:00+00:00")])
        batch = service.open_batch("seq-chn-gold", "batch-1")
        service.lock_batch(batch["batch_id"])

        # 模拟提交执行到一半进程崩溃：事务整体回滚，账本不留半截认定。
        original = service._insert_recognition

        def 崩溃(*args, **kwargs):
            raise RuntimeError("模拟进程崩溃")

        service._insert_recognition = 崩溃
        with self.assertRaises(RuntimeError):
            service.commit_batch(batch["batch_id"])
        service._insert_recognition = original
        self.assertEqual(service.store.query("SELECT * FROM recognitions"), [])

        # 重启后的新服务实例在同一账本上恢复。
        restarted = Service(service.store, clock)
        recovered = restarted.recover_batches()
        self.assertEqual(len(recovered["recovered"]), 1)
        self.assertEqual(
            [a["number"] for a in recovered["recovered"][0]["assignments"]], [1, 2])
        self.assertEqual(len(service.store.query("SELECT * FROM recognitions")), 2)
        # 恢复是幂等的。
        self.assertEqual(restarted.recover_batches()["recovered"], [])


class 隔离与处置测试(unittest.TestCase):
    def test_矛盾结果先隔离再处置(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "k1", [金牌(
            "ed-1974", "ev-m100", athlete="at-su",
            finalized="1974-09-02T10:00:00+00:00", result_id="res-1")])
        认定全部(service)
        milestone = service.milestone("seq-chn-gold", 1)
        self.assertEqual(milestone["published"]["result"]["holder"]["name"], "苏某")

        # 另一来源给出不同持有人：先隔离，有效答案不变。
        clock.推进到(T2)
        outcome = service.import_results("src-b", "k2", [金牌(
            "ed-1974", "ev-m100", athlete="at-li",
            finalized="1974-09-02T10:00:00+00:00")])["outcomes"][0]
        self.assertEqual(outcome["status"], "quarantined")
        self.assertEqual(service.milestone("seq-chn-gold", 1)
                         ["effective"]["result"]["holder"]["name"], "苏某")

        # 维持现状的处置不改变有效链。
        service.resolve_quarantine(outcome["case_id"], "keep_current", "doc-ruling")
        self.assertEqual(service.milestone("seq-chn-gold", 1)
                         ["effective"]["result"]["holder"]["name"], "苏某")
        self.assertEqual(service.list_quarantine(), [])

        # 再次冲突并接受争议主张：追加替代版本，发布记录不覆盖。
        clock.推进到(T3)
        second = service.import_results("src-c", "k3", [金牌(
            "ed-1974", "ev-m100", athlete="at-wang",
            finalized="1974-09-02T10:00:00+00:00")])["outcomes"][0]
        self.assertEqual(second["status"], "quarantined")
        service.resolve_quarantine(second["case_id"], "accept_contender", "doc-ruling")
        milestone = service.milestone("seq-chn-gold", 1)
        self.assertEqual(milestone["published"]["result"]["holder"]["name"], "苏某")
        self.assertEqual(milestone["effective"]["result"]["holder"]["name"], "王某")
        self.assertFalse(milestone["consistent"])

    def test_导入幂等(self):
        service, clock = 建账()
        clock.推进到(T1)
        item = 金牌("ed-1974", "ev-m100", athlete="at-su",
                    finalized="1974-09-02T10:00:00+00:00")
        first = service.import_results("src-a", "same-key", [item])
        second = service.import_results("src-a", "same-key", [item])
        self.assertEqual(first, second)
        self.assertEqual(len(service.store.query("SELECT * FROM results")), 1)
        with self.assertRaises(LedgerError) as ctx:
            service.import_results("src-a", "same-key", [金牌(
                "ed-1974", "ev-w3000", athlete="at-li",
                finalized="1974-09-03T10:00:00+00:00")])
        self.assertEqual(ctx.exception.code, "request_conflict")


class 裁决与双答案测试(unittest.TestCase):
    def 三金入账(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "g1", [金牌(
            "ed-1974", "ev-m100", athlete="at-su",
            finalized="1974-09-02T10:00:00+00:00", result_id="res-g1")])
        service.import_results("src-a", "g2", [金牌(
            "ed-1974", "ev-w3000", athlete="at-li",
            finalized="1974-09-03T10:00:00+00:00", result_id="res-g2")])
        service.import_results("src-a", "g3", [金牌(
            "ed-1978", "ev-w5000", athlete="at-zhao",
            finalized="1978-09-01T10:00:00+00:00", result_id="res-g3")])
        认定全部(service)
        return service, clock

    def test_取消资格后当晚答案与当前答案同时成立(self):
        service, clock = self.三金入账()
        clock.推进到(T2)
        service.record_ruling("disqualification", "doc-ruling",
                              note="赛后兴奋剂取消资格", result_id="res-g2")

        # 当晚发布：第 2 金是李某；当前有效：第 2 金顺移到赵某。
        milestone = service.milestone("seq-chn-gold", 2)
        self.assertEqual(milestone["published"]["result"]["holder"]["name"], "李某")
        self.assertEqual(milestone["published"]["slot_status"], "stripped")
        self.assertEqual(milestone["effective"]["result"]["holder"]["name"], "赵某")
        self.assertEqual(milestone["effective"]["published_number"], 3)
        self.assertFalse(milestone["consistent"])
        self.assertEqual([r["kind"] for r in milestone["rulings"]],
                         ["disqualification"])

        third = service.milestone("seq-chn-gold", 3)
        self.assertEqual(third["published"]["result"]["holder"]["name"], "赵某")
        self.assertIsNone(third["effective"])

        # 指定历史日期的快照仍是当晚的序列。
        snapshot = service.snapshot("seq-chn-gold", T1)
        self.assertEqual(snapshot["count"], 3)
        self.assertEqual(snapshot["entries"][1]["result"]["holder"]["name"], "李某")
        self.assertEqual(service.snapshot("seq-chn-gold", T3)["count"], 2)

        # 被剥夺的槽位保留原编号，不会再抢到新编号。
        later = 认定全部(service, "batch-2")
        self.assertEqual(later["assignments"], [])

    def test_奖牌重分配给队友后两个答案同时成立(self):
        service, clock = self.三金入账()
        clock.推进到(T2)
        service.record_ruling("disqualification", "doc-ruling", result_id="res-g2")
        clock.推进到(T3)
        service.record_ruling(
            "reallocation", "doc-ruling", note="队友递补",
            edition_id="ed-1974", event_id="ev-w3000", medal="gold",
            athlete_id="at-wang")

        milestone = service.milestone("seq-chn-gold", 2)
        self.assertEqual(milestone["published"]["result"]["holder"]["name"], "李某")
        self.assertEqual(milestone["published"]["slot_status"], "replaced")
        self.assertEqual(milestone["effective"]["result"]["holder"]["name"], "王某")
        self.assertEqual(milestone["effective"]["published_number"], 2)
        self.assertFalse(milestone["consistent"])
        self.assertEqual([r["kind"] for r in milestone["rulings"]],
                         ["disqualification", "reallocation"])

        # 统计差异：个人一金易主，代表团与项目总数不变。
        diff = service.stats_diff("seq-chn-gold", T1)
        self.assertEqual(diff["medals_added"], [])
        self.assertEqual(diff["medals_removed"], [])
        self.assertEqual(len(diff["holder_changes"]), 1)
        self.assertEqual(diff["holder_changes"][0]["before"]["name"], "李某")
        self.assertEqual(diff["holder_changes"][0]["after"]["name"], "王某")
        deltas = {d["athlete_id"]: d for d in diff["athlete_delta"]}
        self.assertEqual(deltas["at-li"]["delta"], -1)
        self.assertEqual(deltas["at-wang"]["delta"], 1)
        self.assertEqual(diff["edition_delta"], [])
        self.assertEqual(len(diff["rulings"]), 2)

    def test_统计差异列出奖牌进出与届次变化(self):
        service, clock = self.三金入账()
        clock.推进到(T2)
        service.record_ruling("disqualification", "doc-ruling", result_id="res-g2")
        diff = service.stats_diff("seq-chn-gold", T1)
        self.assertEqual(len(diff["medals_removed"]), 1)
        self.assertEqual(diff["medals_removed"][0]["event_id"], "ev-w3000")
        editions = {d["edition_id"]: d for d in diff["edition_delta"]}
        self.assertEqual(editions["ed-1974"]["before"], 2)
        self.assertEqual(editions["ed-1974"]["after"], 1)
        events = {d["lineage"]: d for d in diff["event_delta"]}
        self.assertEqual(events["ev-w3000"]["delta"], -1)


class 身份与谱系测试(unittest.TestCase):
    def test_同名运动员必须有证据才能合并(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "z1", [金牌(
            "ed-1978", "ev-m100", athlete="at-zhang1",
            finalized="1978-09-02T10:00:00+00:00")])
        service.import_results("src-a", "z2", [金牌(
            "ed-1978", "ev-w3000", athlete="at-zhang2",
            finalized="1978-09-03T10:00:00+00:00")])
        认定全部(service)

        # 同名不自动合并：统计里是两个身份。
        stats = service.stats("seq-chn-gold")
        张伟们 = [a for a in stats["athletes"] if a["name"] == "张伟"]
        self.assertEqual(len(张伟们), 2)

        with self.assertRaises(LedgerError) as ctx:
            service.merge_athletes("at-zhang1", "at-zhang2", "doc-missing")
        self.assertEqual(ctx.exception.code, "evidence_not_found")

        clock.推进到(T2)
        service.merge_athletes("at-zhang1", "at-zhang2", "doc-identity")
        stats = service.stats("seq-chn-gold")
        张伟们 = [a for a in stats["athletes"] if a["name"] == "张伟"]
        self.assertEqual(len(张伟们), 1)
        self.assertEqual(张伟们[0]["athlete_id"], "at-zhang2")
        self.assertEqual(张伟们[0]["count"], 2)

        # 历史快照不受后来的合并影响。
        earlier = service.stats("seq-chn-gold", as_of=T1)
        self.assertEqual(len([a for a in earlier["athletes"] if a["name"] == "张伟"]), 2)

        diff = service.stats_diff("seq-chn-gold", T1)
        deltas = {d["athlete_id"]: d for d in diff["athlete_delta"]}
        self.assertEqual(deltas["at-zhang1"]["delta"], -1)
        self.assertEqual(deltas["at-zhang2"]["delta"], 1)

    def test_项目更名与距离变化不自动视作同一项目(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "e1", [金牌(
            "ed-1974", "ev-w3000", athlete="at-li",
            finalized="1974-09-03T10:00:00+00:00")])
        service.import_results("src-a", "e2", [金牌(
            "ed-1978", "ev-w5000", athlete="at-zhao",
            finalized="1978-09-01T10:00:00+00:00")])
        认定全部(service)

        # 名称与距离都不同：没有谱系链接时是两个统计项目。
        stats = service.stats("seq-chn-gold")
        self.assertEqual(len(stats["events"]), 2)

        with self.assertRaises(LedgerError) as ctx:
            service.link_event_lineage("ev-w3000", "ev-w5000", "doc-missing")
        self.assertEqual(ctx.exception.code, "evidence_not_found")

        clock.推进到(T2)
        service.link_event_lineage("ev-w3000", "ev-w5000", "doc-rename")
        stats = service.stats("seq-chn-gold")
        self.assertEqual(len(stats["events"]), 1)
        lineage = stats["events"][0]
        self.assertEqual(lineage["count"], 2)
        self.assertEqual(sorted(lineage["names"]), ["女子3000米", "女子5000米"])

        # 链接前的快照仍按两个项目统计。
        earlier = service.stats("seq-chn-gold", as_of=T1)
        self.assertEqual(len(earlier["events"]), 2)

    def test_接力阵容计入每位成员(self):
        service, clock = 建账()
        clock.推进到(T1)
        service.import_results("src-a", "r1", [金牌(
            "ed-1978", "ev-m4x100", members=["at-r1", "at-r2", "at-r3", "at-r4"],
            finalized="1978-09-05T10:00:00+00:00")])
        认定全部(service)
        stats = service.stats("seq-chn-gold")
        counts = {a["athlete_id"]: a["count"] for a in stats["athletes"]}
        for member in ("at-r1", "at-r2", "at-r3", "at-r4"):
            self.assertEqual(counts[member], 1)
        milestone = service.milestone("seq-chn-gold", 1)
        holder = milestone["effective"]["result"]["holder"]
        self.assertEqual(holder["kind"], "relay")
        self.assertEqual(len(holder["members"]), 4)

        # 阵容不同则构成冲突，先隔离。
        outcome = service.import_results("src-b", "r2", [金牌(
            "ed-1978", "ev-m4x100", members=["at-r1", "at-r2", "at-r3", "at-r5"],
            finalized="1978-09-05T10:00:00+00:00")])["outcomes"][0]
        self.assertEqual(outcome["status"], "quarantined")


class 接口测试(unittest.TestCase):
    def test_接口端到端与错误信封(self):
        service, clock = 建账()
        clock.推进到(T1)

        def 调用(payload):
            return json.loads(handle(json.dumps(payload, ensure_ascii=False), service))

        调用({"action": "import_results", "source": "src-a", "request_key": "a1",
              "results": [金牌("ed-1974", "ev-m100", athlete="at-su",
                              finalized="1974-09-02T10:00:00+00:00",
                              result_id="res-a1")]})
        调用({"action": "import_results", "source": "src-a", "request_key": "a2",
              "results": [金牌("ed-1974", "ev-w3000", athlete="at-li",
                              finalized="1974-09-03T10:00:00+00:00")]})
        batch = 调用({"action": "open_batch", "sequence_id": "seq-chn-gold",
                      "request_key": "b1"})
        调用({"action": "lock_batch", "batch_id": batch["batch_id"]})
        调用({"action": "commit_batch", "batch_id": batch["batch_id"]})

        answer = 调用({"action": "milestone", "sequence_id": "seq-chn-gold", "number": 1})
        self.assertTrue(answer["consistent"])
        self.assertEqual(answer["published"]["result"]["holder"]["name"], "苏某")

        clock.推进到(T2)
        调用({"action": "record_ruling", "kind": "disqualification",
              "evidence_id": "doc-ruling", "result_id": "res-a1"})
        answer = 调用({"action": "milestone", "sequence_id": "seq-chn-gold", "number": 1})
        self.assertFalse(answer["consistent"])
        self.assertEqual(answer["published"]["result"]["holder"]["name"], "苏某")
        self.assertEqual(answer["effective"]["result"]["holder"]["name"], "李某")
        as_of = 调用({"action": "milestone", "sequence_id": "seq-chn-gold",
                      "number": 1, "as_of": T1})
        self.assertTrue(as_of["consistent"])

        error = 调用({"action": "merge_athletes", "merged_athlete_id": "at-su",
                      "survivor_athlete_id": "at-li", "evidence_id": "doc-missing"})
        self.assertFalse(error["ok"])
        self.assertEqual(error["code"], "evidence_not_found")

        with self.assertRaises(ValueError):
            调用({"action": "no-such-action"})


if __name__ == "__main__":
    unittest.main()
