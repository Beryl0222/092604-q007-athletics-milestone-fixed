# 田径里程碑认定账

把 1974 年至今的田径成绩整理成「里程碑认定账」。核心要回答的问题是：

> 中国田径亚运第 200 金是谁？

这个问题有两个必须同时成立的答案：

- **当晚发布的答案**：决赛当晚按发布顺序，编号被原子分配给某枚金牌，编号永不更改、永不回收。
- **今天有效的答案**：若此后发生取消资格、奖牌重分配或项目更名，按追加的裁决关系算出当前持有者。

赛后裁决**只追加、不覆盖**当年记录，因此两个答案各属其生效日期，凭裁决链同时成立。

## 认定原则

1. **赛事届次 + 项目规则时代决定统计序列。** 项目身份由（项目代码、规则时代、距离/性别/赛制指纹）确定；距离或名称变化**不自动**视作同一项目，只能登记沿革线索用于溯源。
2. **运动员身份沿革。** 同名默认是不同的人；合并身份必须出示已登记证据，否则拒绝。接力按棒次保留完整阵容。
3. **结果版本与证据出处。** 每项认定、每次裁决都必须附带证据（来源 + 定位）。
4. **公开编号只能由锁定批次原子分配。** 编号占用落在 `allocations` 表，`milestone_no` 与 `result_id` 均有唯一约束，一枚奖牌不可能抢到两个编号。
5. **矛盾结果先隔离再处置。** 并行来源对同一届次同一项目给出矛盾金牌时，条目标记为冲突并进隔离队列，提交时跳过，不占编号；人工 `accept`/`discard` 后才继续。
6. **批次中断可从账本恢复。** 锁定即占号（落盘），提交是单事务；进程在锁定后、提交前中断，重开连接 `recover_batch` 沿用原编号继续。已提交批次重复恢复幂等。
7. **追加式裁决。** `vacate`（空缺）/ `reassign`（递补，内含先 vacate 再 reassign 两条效果）/ `reinstate`（恢复），当年的 `assertions` 永不变更。

## 双时间面接口

- `milestone`：返回第 N 枚的 `published`（当晚答案及编号依据）与 `current`（今天有效状态，`official`/`reassigned`/`vacated`），并附 `rulings` 与可直接引用的 `explanation`。
- `snapshot`：指定历史日期的认定快照——当晚序列在那一天的全部有效状态。
- `ruling_impacts`：列出窗口内裁决给**个人、项目、代表团**统计带来的差异（个人 ±1、代表团净变化、结果/项目替代）。
- `person_tally` / `team_tally`：支持 `as_of`，身份合并后自动计入合并后的运动员。

## 主要动作（JSON，经 stdin/`api.handle`）

基础资料：`register_edition`、`register_era`、`register_event`、`link_event_lineage`、
`register_athlete`、`merge_athletes`、`register_source`、`register_evidence`。

导入与发号：`create_batch` → `stage_item`（可多次）→ `open_conflicts` / `resolve_conflict`
→ `lock_batch` → `commit_batch`；中断后 `recover_batch`。另有 `batch_status`、
`register_supplemental_result`（登记原亚军等不占号结果）。

裁决：`adjudicate`、`list_adjudications`。

查询：`milestone`、`snapshot`、`person_tally`、`team_tally`、`ruling_impacts`、`event_lineage`。

写动作可带 `request_key`：同键同载荷重放返回首次响应；同键不同载荷被拒绝（并行导入幂等）。

## 存储

`src/athletics_milestone/store.py` 用 SQLite（仅标准库）建账。关键表：

- `editions` / `rule_eras` / `events`：届次、规则时代、项目身份（含沿革指针，不合并身份）。
- `athletes`：身份与有证据的合并链。
- `sources` / `evidence`：出处。
- `batches` / `batch_items` / `allocations`：批次、暂存项、原子编号占用（恢复依据）。
- `results` / `relay_lineups` / `assertions`：结果、接力阵容、当晚不可变认定。
- `adjudications` / `adjudication_effects`：追加式裁决及其逐条效果。
- `conflict_queue`：矛盾结果隔离区。
- `records` / `request_receipts`：既有基础登记与幂等收据。

`results` 上有部分唯一索引 `one_gold_per_event`：即使并行冲突检测同时漏判，数据库也会在提交第二枚同届同项金牌时原子拒绝。

## 运行

运行测试：

```
python3 -m pytest -q          # 若安装了 pytest
PYTHONPATH=src python3 -m unittest discover -s tests
```

检查源码：`python3 -m compileall src`

本地冒烟：`printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m athletics_milestone.cli`

完整业务场景见 `tests/test_milestone_ledger.py`：构造 1974 年起 199 枚历史金牌后，
第 200 金当晚编号、并行矛盾隔离、剥夺与递补、当晚/今天双答案、同名合并证据门槛、
项目更名不接续、并行发号不重号、跨进程中断恢复、接口幂等。

项目只使用 Python 标准库，运行期间不连接其他服务。
