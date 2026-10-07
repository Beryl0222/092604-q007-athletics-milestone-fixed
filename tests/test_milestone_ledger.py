import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from athletics_milestone.api import handle
from athletics_milestone.ledger import LedgerError, MilestoneLedger
from athletics_milestone.service import Service
from athletics_milestone.store import Store


def 建账():
    ledger = MilestoneLedger(Store())
    ledger.register_source("src-official", kind="official", title="亚运会官方成绩册",
                           publisher="OCA")
    ledger.register_source("src-court", kind="ruling", title="体育仲裁裁决书", publisher="CAS")
    ledger.register_evidence("ev-results", source_id="src-official", locator="成绩册卷一")
    ledger.register_evidence("ev-court", source_id="src-court", locator="CAS-2024-001")
    ledger.register_era("era-modern", "athletics", "现代规则", "1974-01-01")
    return ledger


def 种子历史(ledger, n=199):
    """造 n 枚历史金牌：20 个届次 × 10 个项目，编号 1..n。"""
    for i in range(1, 21):
        ledger.register_edition(f"e-{i:02d}", f"第{i}届", 1974 + (i - 1) * 2, games_no=i)
    for j in range(1, 11):
        ledger.register_event(f"ev-{j:02d}", f"M{j}", f"男子项目{j}", "athletics",
                              f"spec-{j}", "era-modern")
    # 两个“张伟”先按不同身份登记
    ledger.register_athlete("zhang-a", "张伟", birth_date="1989-05-05")
    ledger.register_athlete("zhang-b", "张伟", birth_date="1992-08-08")
    batch_id = "b-history"
    ledger.create_batch(batch_id, note="1974 年至今历史成绩")
    for k in range(n):
        no = k + 1
        edition = f"e-{(k // 10) + 1:02d}"
        event = f"ev-{(k % 10) + 1:02d}"
        if no == 50:
            athlete = "zhang-a"
        elif no == 60:
            athlete = "zhang-b"
        else:
            athlete = f"a-{no:03d}"
            ledger.register_athlete(athlete, f"选手{no}")
        ledger.stage_item(batch_id, {
            "result_id": f"r-{no:03d}", "edition_id": edition, "event_id": event,
            "payload": {"result_date": (date(1974, 9, 1) + timedelta(days=k)).isoformat()
                        + "T10:00",
                        "athlete_id": athlete, "evidence_id": "ev-results"}})
    out = ledger.commit_batch(batch_id)
    assert [c["milestone_no"] for c in out["committed"]] == list(range(1, n + 1))
    return out


class 第200金场景测试(unittest.TestCase):
    def setUp(self):
        self.ledger = 建账()
        种子历史(self.ledger, 199)
        # 当晚的第 200 金：男子 4×100 米接力，中国队
        for aid, name in [("liu", "刘甲"), ("guan", "关乙"), ("zhang-w3", "张丙"),
                          ("wang", "王丁"), ("lee", "李一"), ("park", "朴二"),
                          ("choi", "崔三"), ("kim", "金四")]:
            self.ledger.register_athlete(aid, name)
        self.ledger.create_batch("b-200", note="杭州第19届亚运会当晚")
        staged = self.ledger.stage_item("b-200", {
            "result_id": "r-chn-relay", "edition_id": "e-20", "event_id": "ev-10",
            "payload": {"result_date": "2023-10-03T20:00", "team_code": "CHN",
                        "lineup": ["liu", "guan", "zhang-w3", "wang"],
                        "performance": "38.99", "evidence_id": "ev-results",
                        "published_note": "决赛当晚按冲线顺序发布，第200枚编号原子分配给中国队"}})
        self.assertEqual(staged["state"], "staged")
        self.committed = self.ledger.commit_batch("b-200")

    def test_当晚发布编号为200且不可更改(self):
        numbers = [c["milestone_no"] for c in self.committed["committed"]]
        self.assertEqual(numbers, [200])
        view = self.ledger.milestone(200, as_of="2023-10-03T23:00")
        self.assertEqual(view["published"]["winner"]["team_code"], "CHN")
        self.assertEqual([a["display_name"] for a in view["published"]["winner"]["lineup"]],
                         ["刘甲", "关乙", "张丙", "王丁"])
        self.assertEqual(view["published"]["winner"]["performance"], "38.99")
        self.assertFalse(view["answers_coexist"])  # 当时尚无裁决

    def test_并行来源矛盾先隔离且不占编号(self):
        self.ledger.create_batch("b-parallel")
        conflict = self.ledger.stage_item("b-parallel", {
            "result_id": "r-kor-night", "edition_id": "e-20", "event_id": "ev-10",
            "payload": {"result_date": "2023-10-03T20:00", "team_code": "KOR",
                        "lineup": ["lee", "park", "choi", "kim"],
                        "evidence_id": "ev-results"}})
        self.assertEqual(conflict["state"], "conflict")
        open_conflicts = self.ledger.open_conflicts()
        self.assertEqual(len(open_conflicts), 1)
        self.assertIn("矛盾", open_conflicts[0]["reason"])
        # 隔离条目提交时跳过，绝不占用任何编号
        out = self.ledger.commit_batch("b-parallel")
        self.assertEqual(out["committed"], [])
        self.assertEqual(len(out["quarantined"]), 1)
        self.assertEqual(self.ledger.store.max_milestone_no(), 200)

    def test_同一枚奖牌不能拿到两个编号(self):
        self.ledger.create_batch("b-dup")
        again = self.ledger.stage_item("b-dup", {
            "result_id": "r-chn-relay", "edition_id": "e-20", "event_id": "ev-10",
            "payload": {"result_date": "2023-10-03T20:00", "team_code": "CHN",
                        "lineup": ["liu", "guan", "zhang-w3", "wang"],
                        "evidence_id": "ev-results"}})
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["milestone_no"], 200)
        self.ledger.commit_batch("b-dup")
        self.assertEqual(self.ledger.store.max_milestone_no(), 200)

    def test_赛后剥夺与递补_当晚与今天两个答案同时成立(self):
        # 2024-05：中国队因兴奋剂被剥夺，金牌递补韩国队（原亚军）
        ruling = self.ledger.adjudicate(
            "adj-200", "reassign", 200, adjudicated_at="2024-05-01T09:00",
            reason="中国队接力成员兴奋剂检测阳性，名次取消，韩国队递补",
            evidence_id="ev-court",
            to_result={"result_id": "r-kor-runnerup", "edition_id": "e-20",
                       "event_id": "ev-10", "result_date": "2023-10-03T20:00",
                       "team_code": "KOR", "performance": "39.10",
                       "lineup": ["lee", "park", "choi", "kim"]})
        self.assertEqual([e["effect"] for e in ruling["effects"]], ["vacate", "reassign"])

        night = self.ledger.snapshot("2023-10-03T23:00")
        self.assertEqual(night["count"], 200)
        self.assertEqual(night["medals"][-1]["holder"]["team_code"], "CHN")  # 当晚仍是中国

        view = self.ledger.milestone(200)
        self.assertEqual(view["published"]["winner"]["team_code"], "CHN")     # 当年记录未被覆盖
        self.assertEqual(view["current"]["status"], "reassigned")
        self.assertEqual(view["current"]["holder"]["team_code"], "KOR")
        self.assertEqual([a["display_name"] for a in view["current"]["holder"]["lineup"]],
                         ["李一", "朴二", "崔三", "金四"])
        self.assertTrue(view["answers_coexist"])
        self.assertIn("CHN", view["explanation"])
        self.assertIn("KOR", view["explanation"])
        self.assertEqual(len(view["rulings"]), 1)

        # 个人统计随裁决变化
        self.assertEqual(self.ledger.person_tally("liu", "2023-10-03T23:00")["gold_count"], 1)
        self.assertEqual(self.ledger.person_tally("lee")["gold_count"], 1)
        self.assertEqual(self.ledger.person_tally("liu")["gold_count"], 0)
        # 代表团统计：CHN -1，KOR +1
        self.assertEqual(self.ledger.team_tally("CHN", "2023-10-03T23:00")["gold_count"], 1)
        self.assertEqual(self.ledger.team_tally("CHN")["gold_count"], 0)
        impacts = self.ledger.ruling_impacts(since="2023-10-03T23:00")
        teams = {r["team_code"]: r["delta"] for r in impacts["team_tally_delta"]}
        self.assertEqual(teams, {"CHN": -1, "KOR": 1})
        persons = {(p["athlete_id"], p["delta"]) for p in impacts["person_changes"]}
        self.assertIn(("liu", -1), persons)
        self.assertIn(("lee", 1), persons)
        ev_change = [e for e in impacts["event_changes"] if e["milestone_no"] == 200][0]
        self.assertEqual(ev_change["from_result_id"], "r-chn-relay")
        self.assertEqual(ev_change["to_result_id"], "r-kor-runnerup")

    def test_纯空缺与恢复裁决(self):
        self.ledger.adjudicate("adj-1-v", "vacate", 1, adjudicated_at="2024-01-01",
                               reason="历史复核取消", evidence_id="ev-court")
        mid = self.ledger.milestone(1, as_of="2024-03-01")
        self.assertEqual(mid["current"]["status"], "vacated")
        self.assertIsNone(mid["current"]["holder"])
        snap = self.ledger.snapshot("2024-03-01")
        self.assertEqual(snap["medals"][0]["status"], "vacated")
        self.assertEqual(snap["count"], 200)  # 编号不回收

        self.ledger.adjudicate("adj-1-r", "reinstate", 1, adjudicated_at="2024-06-01",
                               reason="上诉成功恢复名次", evidence_id="ev-court")
        self.assertEqual(self.ledger.milestone(1)["current"]["status"], "official")

    def test_同名运动员无证据不能合并_有证据才合并(self):
        with self.assertRaises(LedgerError):
            self.ledger.merge_athletes("zhang-b", "zhang-a", evidence_id="")
        with self.assertRaises(LedgerError):
            self.ledger.merge_athletes("zhang-b", "zhang-a", evidence_id="ev-missing")
        # 未合并前两人各算各的
        self.assertEqual(self.ledger.person_tally("zhang-a")["milestone_numbers"], [50])
        self.assertEqual(self.ledger.person_tally("zhang-b")["milestone_numbers"], [60])
        self.ledger.register_evidence("ev-id", source_id="src-official",
                                      locator="身份证与注册档比对")
        self.ledger.merge_athletes("zhang-b", "zhang-a", evidence_id="ev-id")
        tally = self.ledger.person_tally("zhang-b")
        self.assertEqual(tally["athlete_id"], "zhang-a")
        self.assertEqual(tally["milestone_numbers"], [50, 60])
        # 第60金的当晚记录仍写原名，同时标注身份沿革与证据
        holder = self.ledger.milestone(60)["published"]["winner"]["athlete"]
        self.assertEqual(holder["athlete_id"], "zhang-a")
        self.assertEqual(holder["recorded_as"]["athlete_id"], "zhang-b")
        self.assertEqual(holder["recorded_as"]["merged_evidence"], "ev-id")

    def test_距离或名称变化不自动成为同一项目(self):
        self.ledger.register_era("era-old", "athletics", "女子长跑旧设项", "1974-01-01",
                                 "1993-12-31")
        self.ledger.register_era("era-new", "athletics", "女子长跑新设项", "1994-01-01")
        self.ledger.register_event("ev-3000w", "W3000", "女子3000米", "athletics",
                                   "3000m:track", "era-old")
        self.ledger.register_event("ev-5000w", "W5000", "女子5000米", "athletics",
                                   "5000m:track", "era-new")
        self.ledger.link_event_lineage("ev-3000w", "ev-5000w", note="1994 年设项调整")
        lineage = self.ledger.event_lineage("ev-3000w")
        self.assertEqual([e["event_id"] for e in lineage["events"]],
                         ["ev-3000w", "ev-5000w"])
        self.assertFalse(lineage["same_identity"])
        # 两个身份各自出现在序列中，统计互不相通
        self.ledger.create_batch("b-rename")
        self.ledger.stage_item("b-rename", {
            "result_id": "r-5000w", "edition_id": "e-20", "event_id": "ev-5000w",
            "payload": {"result_date": "2023-10-02T19:00", "athlete_id": "a-001",
                        "evidence_id": "ev-results"}})
        out = self.ledger.commit_batch("b-rename")
        self.assertEqual(out["committed"][0]["milestone_no"], 201)

    def test_并行锁定批次编号不重叠(self):
        self.ledger.register_edition("e-x1", "特别届一", 2025)
        self.ledger.register_edition("e-x2", "特别届二", 2026)
        self.ledger.register_event("ev-x1", "X1", "特设项一", "athletics", "x1", "era-modern")
        self.ledger.register_event("ev-x2", "X2", "特设项二", "athletics", "x2", "era-modern")
        self.ledger.register_athlete("ax1", "特设选手一")
        self.ledger.register_athlete("ax2", "特设选手二")
        self.ledger.create_batch("b-a")
        self.ledger.create_batch("b-b")
        self.ledger.stage_item("b-a", {"result_id": "r-x1", "edition_id": "e-x1",
                                       "event_id": "ev-x1",
                                       "payload": {"result_date": "2025-05-01", "athlete_id": "ax1",
                                                   "evidence_id": "ev-results"}})
        self.ledger.stage_item("b-b", {"result_id": "r-x2", "edition_id": "e-x2",
                                       "event_id": "ev-x2",
                                       "payload": {"result_date": "2026-05-01", "athlete_id": "ax2",
                                                   "evidence_id": "ev-results"}})
        a_locked = self.ledger.lock_batch("b-a")
        b_locked = self.ledger.lock_batch("b-b")
        a_nos = {x["milestone_no"] for x in a_locked["allocations"]}
        b_nos = {x["milestone_no"] for x in b_locked["allocations"]}
        self.assertTrue(a_nos.isdisjoint(b_nos))
        self.assertEqual(a_nos | b_nos, {201, 202})
        self.ledger.commit_batch("b-a")
        self.ledger.commit_batch("b-b")
        self.assertEqual(self.ledger.store.max_milestone_no(), 202)

    def test_裁决必须附证据(self):
        with self.assertRaises(LedgerError):
            self.ledger.adjudicate("adj-x", "vacate", 200, evidence_id="ev-nope",
                                   adjudicated_at="2024-01-01")

    def test_认定条目必须附证据(self):
        self.ledger.create_batch("b-noev")
        with self.assertRaises(LedgerError):
            self.ledger.stage_item("b-noev", {
                "result_id": "r-noev", "edition_id": "e-20", "event_id": "ev-09",
                "payload": {"result_date": "2023-10-04", "athlete_id": "a-001"}})


class 中断恢复测试(unittest.TestCase):
    def test_锁定后进程中断_新进程照原编号恢复提交(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.db"
            ledger = MilestoneLedger(Store(db))
            ledger.register_era("era-modern", "athletics", "现代规则", "1974-01-01")
            ledger.register_edition("e-1", "第一届", 2023)
            ledger.register_event("ev-1", "M1", "项目一", "athletics", "s1", "era-modern")
            ledger.register_source("s1", title="官方")
            ledger.register_evidence("ev1", source_id="s1")
            ledger.register_athlete("p1", "选手甲")
            ledger.register_athlete("p2", "选手乙")
            ledger.register_event("ev-2", "M2", "项目二", "athletics", "s2", "era-modern")
            ledger.create_batch("b-1")
            ledger.stage_item("b-1", {"result_id": "r-1", "edition_id": "e-1",
                                      "event_id": "ev-1",
                                      "payload": {"result_date": "2023-09-01", "athlete_id": "p1",
                                                  "evidence_id": "ev1"}})
            ledger.stage_item("b-1", {"result_id": "r-2", "edition_id": "e-1",
                                      "event_id": "ev-2",
                                      "payload": {"result_date": "2023-09-02", "athlete_id": "p2",
                                                  "evidence_id": "ev1"}})
            locked = ledger.lock_batch("b-1")
            self.assertEqual([a["milestone_no"] for a in locked["allocations"]], [1, 2])

            # 模拟进程重启：另开连接从账本恢复
            ledger2 = MilestoneLedger(Store(db))
            recovered = ledger2.recover_batch("b-1")
            self.assertEqual(recovered["state"], "committed")
            self.assertEqual([c["milestone_no"] for c in recovered["committed"]], [1, 2])
            self.assertTrue(recovered["recovered"])
            self.assertEqual(ledger2.milestone(1)["published"]["winner"]["athlete"]["display_name"],
                             "选手甲")

            # 再重启一次：已提交批次重复恢复是幂等的
            ledger3 = MilestoneLedger(Store(db))
            again = ledger3.recover_batch("b-1")
            self.assertEqual(again["state"], "committed")
            self.assertEqual(ledger3.store.max_milestone_no(), 2)


class JSON接口测试(unittest.TestCase):
    def setUp(self):
        self.service = Service(Store())
        ledger = self.service.ledger
        ledger.register_era("era-modern", "athletics", "现代规则", "1974-01-01")
        ledger.register_edition("e-1", "第一届", 2023)
        ledger.register_event("ev-1", "M1", "项目一", "athletics", "s1", "era-modern")
        ledger.register_source("s1", title="官方")
        ledger.register_evidence("ev1", source_id="s1")
        ledger.register_athlete("p1", "选手甲")

    def _call(self, body):
        return json.loads(handle(json.dumps(body, ensure_ascii=False), self.service))

    def test_第200金双答案接口(self):
        self._call({"action": "create_batch", "batch_id": "b"})
        self._call({"action": "stage_item", "batch_id": "b", "item": {
            "result_id": "r-200", "edition_id": "e-1", "event_id": "ev-1",
            "payload": {"result_date": "2023-10-03T20:00", "athlete_id": "p1",
                        "evidence_id": "ev1",
                        "published_note": "当晚发布为中国田径亚运第200金"}}})
        self._call({"action": "commit_batch", "batch_id": "b"})
        night = self._call({"action": "milestone", "milestone_no": 1,
                            "as_of": "2023-10-03T21:00"})
        self.assertEqual(night["published"]["winner"]["athlete"]["display_name"], "选手甲")
        today = self._call({"action": "milestone", "milestone_no": 1})
        self.assertEqual(today["current"]["holder"]["athlete"]["display_name"], "选手甲")
        snap = self._call({"action": "snapshot", "as_of": "2023-10-03T21:00"})
        self.assertEqual(snap["count"], 1)

    def test_请求幂等_重放同响应_篡改被拒(self):
        body = {"action": "create_batch", "batch_id": "b-idem", "request_key": "k-1"}
        first = self._call(body)
        second = self._call(body)
        self.assertEqual(first, second)
        with self.assertRaises(LedgerError):
            self._call({"action": "create_batch", "batch_id": "b-tampered",
                        "request_key": "k-1"})


if __name__ == "__main__":
    unittest.main()
