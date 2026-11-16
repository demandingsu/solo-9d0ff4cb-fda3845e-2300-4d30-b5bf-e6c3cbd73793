"""Pydantic 请求 / 响应模型。

矩形统一用左下角、右上角两个整数点表示：rect = [x1, y1, x2, y2]，
要求 x1 < x2、y1 < y2，宽为 x2-x1、高为 y2-y1，面积为整数。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Kind = Literal["old", "new"]


class Rect(BaseModel):
    """整数轴对齐矩形 [x1, y1, x2, y2]，同时接受数组与对象两种 JSON 形式。"""

    x1: int
    y1: int
    x2: int
    y2: int

    @model_validator(mode="before")
    @classmethod
    def _accept_array_form(cls, data):
        # 支持 "rect": [x1, y1, x2, y2] 的简写
        if isinstance(data, (list, tuple)):
            if len(data) != 4:
                raise ValueError("矩形数组必须恰好包含 4 个整数 [x1, y1, x2, y2]")
            return dict(zip(("x1", "y1", "x2", "y2"), data))
        return data

    @model_validator(mode="after")
    def _check_positive_extent(self) -> "Rect":
        if self.x1 >= self.x2 or self.y1 >= self.y2:
            raise ValueError(
                f"矩形须满足 x1 < x2 且 y1 < y2，收到 [{self.x1}, {self.y1}, {self.x2}, {self.y2}]"
            )
        return self

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)


class OldGridItem(BaseModel):
    """旧网格条目：编号、整数矩形、整数户数。"""

    id: str = Field(..., min_length=1, description="旧网格编号，非空字符串")
    rect: Rect
    households: int = Field(..., ge=0, description="整数户数，非负")


class NewDistrictItem(BaseModel):
    """新片区条目：编号、整数矩形。"""

    id: str = Field(..., min_length=1, description="新片区编号，非空字符串")
    rect: Rect


class OldGridSubmission(BaseModel):
    """旧网格整版提交。"""

    items: list[OldGridItem] = Field(..., description="整版条目，最多 12 个")

    @field_validator("items")
    @classmethod
    def _check_size(cls, v: list[OldGridItem]) -> list[OldGridItem]:
        # 具体上限在服务层按配置复核，这里先给出明确的 422 错误
        from .config import MAX_ITEMS_PER_SUBMISSION

        if len(v) > MAX_ITEMS_PER_SUBMISSION:
            raise ValueError(f"每版最多 {MAX_ITEMS_PER_SUBMISSION} 个条目，收到 {len(v)} 个")
        return v


class NewDistrictSubmission(BaseModel):
    """新片区整版提交。"""

    items: list[NewDistrictItem] = Field(..., description="整版条目，最多 12 个")

    @field_validator("items")
    @classmethod
    def _check_size(cls, v: list[NewDistrictItem]) -> list[NewDistrictItem]:
        from .config import MAX_ITEMS_PER_SUBMISSION

        if len(v) > MAX_ITEMS_PER_SUBMISSION:
            raise ValueError(f"每版最多 {MAX_ITEMS_PER_SUBMISSION} 个条目，收到 {len(v)} 个")
        return v


class VerifiedHouseholdItem(BaseModel):
    """外部核定供数条目：片区编号 + 非负整数核定户数。"""

    id: str = Field(..., min_length=1, description="片区编号，须与当前任务片区编号一致")
    households: int = Field(..., ge=0, description="核定户数，非负整数")


class VerifiedSubmission(BaseModel):
    """片区核定户数整版提交。"""

    items: list[VerifiedHouseholdItem] = Field(
        ..., description="整版核定条目，编号唯一且覆盖当前任务的全部片区"
    )

    @field_validator("items")
    @classmethod
    def _check_size(cls, v: list[VerifiedHouseholdItem]) -> list[VerifiedHouseholdItem]:
        from .config import MAX_ITEMS_PER_SUBMISSION

        if len(v) > MAX_ITEMS_PER_SUBMISSION:
            raise ValueError(f"每版最多 {MAX_ITEMS_PER_SUBMISSION} 个条目，收到 {len(v)} 个")
        if len(v) == 0:
            raise ValueError("提交内容不能为空")
        ids = [it.id for it in v]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"片区编号必须唯一，重复编号：{', '.join(dupes)}")
        return v


class CellOut(BaseModel):
    """持久化后的条目视图。"""

    id: str
    x1: int
    y1: int
    x2: int
    y2: int
    area: int
    households: int | None = None


class VersionOut(BaseModel):
    """输入版本视图。"""

    version: int
    kind: Kind
    created_at: str
    item_count: int
    items: list[CellOut]


class AllocationOut(BaseModel):
    """单个旧格对单个片区的分摊明细。"""

    district_id: str
    old_id: str
    overlap_area: int
    households: int


class DistrictSummaryOut(BaseModel):
    """片区汇总。"""

    district_id: str
    allocated_households: int
    detail: list[AllocationOut]


class GenerationOut(BaseModel):
    """生成任务视图。"""

    id: int
    status: Literal["pending", "running", "succeeded", "failed"]
    old_version: int | None
    new_version: int | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


class CurrentResultOut(BaseModel):
    """当前已发布结果：片区汇总 + 两份输入版本。"""

    generation_id: int
    published_at: str
    total_households: int
    old_version: VersionOut
    new_version: VersionOut
    districts: list[DistrictSummaryOut]


class VerificationFlowOut(BaseModel):
    """核定相容时保存的逐格整数流向。"""

    old_id: str
    district_id: str
    households: int


class VerificationOldTotalOut(BaseModel):
    """单个旧格的核定流向汇总（旧格侧合计应等于原户数）。"""

    old_id: str
    households: int


class VerificationDistrictTotalOut(BaseModel):
    """单个片区的核定户数汇总（片区侧合计应等于外部核定）。"""

    district_id: str
    households: int


class VerificationOut(BaseModel):
    """核定供数记录视图（成功/失败均返回此结构，失败时诊断字段有值）。"""

    id: int
    generation_id: int
    old_version: int
    new_version: int
    status: Literal["feasible", "infeasible"]
    total_households: int
    submitted_at: str
    demands: list[VerificationDistrictTotalOut]
    flows: list[VerificationFlowOut] = []
    blocked_old_ids: list[str] = []
    reachable_district_ids: list[str] = []
    shortfall: int | None = None
    old_totals: list[VerificationOldTotalOut] = []
    district_totals: list[VerificationDistrictTotalOut] = []


class InfeasibleVerificationOut(BaseModel):
    """总量相同却不可行时的结构化诊断（HTTP 422 的响应体）。"""

    message: str
    verification_id: int
    generation_id: int
    old_version: int
    new_version: int
    total_households: int
    blocked_old_ids: list[str]
    reachable_district_ids: list[str]
    shortfall: int


class HealthOut(BaseModel):
    status: str
    db: str
