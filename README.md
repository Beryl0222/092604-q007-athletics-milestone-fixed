# 田径里程碑认定

本项目提供田径里程碑认定的服务端基础：把 1974 年至今的成绩资料整理成一本**里程碑认定账**，用来回答"中国田径亚运第 N 金是谁"这类问题——既给今天认可的答案，也保留当晚发布时的编号依据；取消资格、奖牌重分配、项目更名之后，两个答案同时成立、各自可查。

## 领域模型

- **届次（editions）**：赛事届次，如 1974 年德黑兰亚运会。
- **项目（events）**：某一规则时代下的项目定义。更名或距离变化产生新的项目定义；是否视同同一统计项目，必须凭证据显式建立**谱系链接（event_lineage_links）**，名称或距离相近不会自动合并。
- **运动员（athletes）**：同名不同人各自持有身份。合并必须附证据（**athlete_merges**），且只追加不改写；历史快照只应用截止日期前已记录的合并。
- **接力阵容（relay_lineups / relay_members）**：接力奖牌记录完整棒次阵容，统计计入每位成员。
- **结果版本（results）**：一枚奖牌结果的一个版本。原始发布是版本链起点，赛后裁决产生的新版本通过 `supersedes` 串成只增不改的链，当年记录保持原样。
- **证据（evidence）**：每条结果、每次裁决、每次合并与谱系链接都必须登记出处。
- **统计序列（sequences）**：由代表团 + 奖牌种类 + 届次集合 + 项目（规则时代）集合定义；赛事届次和项目规则时代共同决定统计口径。
- **批次（batches / batch_events / batch_slots）**：公开编号只能由锁定批次分配。`open → lock → commit`，提交在单个事务内原子完成。
- **认定（recognitions）**：编号、槽位（届次, 项目, 奖牌）与发布所依据的结果版本一并锁定，永不回收、永不改写。
- **裁决（rulings）**：取消资格追加剥夺标记，重分配追加新版本；以追加替代关系表达，不覆盖当年记录。
- **隔离区（quarantine_cases / quarantine_resolutions）**：多来源并行导入时，矛盾结果先隔离，处置（维持现状 / 接受争议主张）之后才进入有效版本链。

## 账本不变量

1. 结果版本、裁决、身份合并与谱系链接只追加，不覆盖历史行。
2. 公开编号只能由锁定批次在单个写事务内原子分配。
3. 同一统计序列内，一个奖牌槽位至多持有一个编号——任何一枚奖牌都抢不到两个编号（数据库唯一约束 + 串行化写事务保证）。
4. 冲突结果先隔离，处置之后才允许进入有效版本链。
5. 批次中断（锁定未提交）从账本恢复；提交本身幂等，重复恢复不会重复编号。

## "第 200 金"怎样回答

`milestone` 一次返回两个答案：

- `published`：当晚发布的认定——编号、发布时依据的结果版本、批次与时间，以及该槽位此后的状态（`effective` / `replaced` / `stripped`）。
- `effective`：当前（或 `as_of` 指定日期）有效的第 N 枚——按当时账本状态重排有效序列后取第 N 位，并给出它自己的发布编号。
- `rulings`：发布之后影响该槽位的裁决链；`consistent` 表示两个答案是否仍指向同一结果。

配套查询：`snapshot` 给出指定历史日期的完整认定快照；`stats` 给出个人 / 项目谱系 / 代表团届次统计；`stats_diff` 列出某日期以来后续裁决带来的奖牌进出、持有人变化与三类统计差异。

## JSON 接口

进程内入口 `athletics_milestone.api.handle(raw)`，动作包括：

| 动作 | 说明 |
| --- | --- |
| `add_edition` / `add_event` / `add_athlete` / `add_evidence` | 登记届次、项目、运动员、证据 |
| `define_sequence` / `sequence_add_edition` / `sequence_add_event` | 定义统计序列及其口径 |
| `import_results` | 幂等导入（请求键去重），矛盾结果自动隔离 |
| `open_batch` / `lock_batch` / `commit_batch` / `recover_batches` | 批次编号与中断恢复 |
| `record_ruling` | 登记裁决（`disqualification` / `reallocation`） |
| `resolve_quarantine` / `list_quarantine` | 处置与查看隔离案件 |
| `merge_athletes` / `link_event_lineage` | 凭证据合并身份、连接项目谱系 |
| `milestone` / `snapshot` / `stats` / `stats_diff` | 第 N 枚双答案、历史快照、统计与差异 |

领域错误返回 `{"ok": false, "code", "error"}` 信封，不抛异常出边界。

## 目录

- `src/athletics_milestone/domain.py` 领域对象与账本约束。
- `src/athletics_milestone/store.py` SQLite 表结构与串行化写事务。
- `src/athletics_milestone/service.py` 导入、编号、裁决、合并与查询行为。
- `src/athletics_milestone/api.py` 进程内 JSON 请求边界。
- `tests/` 覆盖账本不变量与双答案场景。

## 运行

运行测试：`PYTHONPATH=src python3 -m unittest discover -s tests`（或 `python3 -m pytest -q`，如已安装 pytest）

检查源码：`python3 -m compileall src`

本地冒烟：`printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m athletics_milestone.cli`

项目只使用 Python 标准库，运行期间不连接其他服务。
