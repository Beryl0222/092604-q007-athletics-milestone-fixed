"""田径里程碑认定的领域对象与账本约束。"""
from dataclasses import dataclass


class LedgerError(Exception):
    """账本业务约束被拒绝时抛出，code 供调用方区分处置方式。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Record:
    """早期骨架保留的通用登记对象。"""

    record_id: str
    owner_id: str
    state: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class ResultVersion:
    """一枚奖牌结果的一个版本。

    版本只追加不修改：原始发布是版本链的起点，赛后裁决产生的新版本
    通过 supersedes 指向前一版，当年的记录保持原样可查。
    """

    result_id: str
    edition_id: str
    event_id: str
    medal: str
    delegation: str
    holder_kind: str  # athlete | relay
    athlete_id: str | None
    lineup_id: str | None
    holder_key: str
    finalized_on: str
    supersedes: str | None
    evidence_id: str
    source: str
    recorded_at: str

    @classmethod
    def from_row(cls, row: dict) -> "ResultVersion":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def slot(self) -> tuple[str, str, str]:
        """奖牌槽位：（届次, 项目, 奖牌）。"""
        return (self.edition_id, self.event_id, self.medal)


@dataclass(frozen=True)
class Recognition:
    """一次公开认定：编号、槽位与发布所依据的结果版本一并锁定。"""

    sequence_id: str
    number: int
    edition_id: str
    event_id: str
    medal: str
    published_result_id: str
    batch_id: str
    recorded_at: str

    @classmethod
    def from_row(cls, row: dict) -> "Recognition":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def slot(self) -> tuple[str, str, str]:
        return (self.edition_id, self.event_id, self.medal)


def athlete_holder_key(athlete_id: str) -> str:
    """个人持有人的指纹，用于冲突检测。"""
    return f"athlete:{athlete_id}"


def relay_holder_key(members: list[tuple[int, str]]) -> str:
    """接力阵容的指纹：按棒次排序的（棒次, 运动员）序列。"""
    ordered = sorted(members)
    return "relay:" + ",".join(f"{leg}:{athlete_id}" for leg, athlete_id in ordered)
