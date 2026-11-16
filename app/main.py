"""FastAPI 应用：输入版本提交、异步生成、结果查询。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query
from fastapi.responses import RedirectResponse
from sqlite3 import Connection

from . import db
from .config import DB_PATH, WORKER_THREADS
from .models import (
    CurrentResultOut,
    GenerationOut,
    HealthOut,
    InfeasibleVerificationOut,
    NewDistrictSubmission,
    OldGridSubmission,
    VerificationOut,
    VerifiedSubmission,
    VersionOut,
)

# 单线程工作池：生成任务按提交顺序串行执行与发布
_executor: ThreadPoolExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _executor
    db.init_db()
    _executor = ThreadPoolExecutor(
        max_workers=WORKER_THREADS, thread_name_prefix="generation"
    )
    yield
    _executor.shutdown(wait=True)
    _executor = None


app = FastAPI(
    title="城市统计片区重划分摊服务",
    description=(
        "整版提交旧网格（编号、整数矩形、户数）与新片区（编号、整数矩形），"
        "按重叠面积以最大余数法逐格分摊户数并守恒总户数。"
        "生成任务固定提交时刻的版本快照，生成期间可继续替换任一输入。"
        "另支持外部核定供数：以最大流判定片区核定户数与已发布旧网格的整数相容，"
        "相容时保存逐格整数流向，不可行时给出受限旧格、可达片区与缺额诊断。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def get_db() -> Connection:
    conn = db.get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _items_from_old(payload: OldGridSubmission) -> list[dict]:
    return [
        {
            "id": it.id,
            "rect": it.rect.as_tuple(),
            "households": it.households,
        }
        for it in payload.items
    ]


def _items_from_new(payload: NewDistrictSubmission) -> list[dict]:
    return [{"id": it.id, "rect": it.rect.as_tuple()} for it in payload.items]


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["运维"])
def health(conn: Connection = Depends(get_db)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "db": str(DB_PATH)}


@app.post("/old-grids", response_model=VersionOut, status_code=201, tags=["输入版本"])
def submit_old_grid(payload: OldGridSubmission, conn: Connection = Depends(get_db)):
    """整版提交旧网格；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("old", _items_from_old(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.post(
    "/new-districts", response_model=VersionOut, status_code=201, tags=["输入版本"]
)
def submit_new_districts(
    payload: NewDistrictSubmission, conn: Connection = Depends(get_db)
):
    """整版提交新片区；替换旧输入即产生新版本（历史版本保留）。"""
    try:
        with conn:
            version_id = db.insert_version("new", _items_from_new(payload), conn)
    except db.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return db.get_version(version_id, conn)


@app.get("/versions", response_model=list[VersionOut], tags=["输入版本"])
def list_versions(
    kind: str | None = Query(None, pattern="^(old|new)$"),
    conn: Connection = Depends(get_db),
):
    """列出输入版本，可按 kind=old/new 过滤。"""
    return db.list_versions(kind, conn)


@app.get("/versions/{version_id}", response_model=VersionOut, tags=["输入版本"])
def get_version(
    version_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    try:
        return db.get_version(version_id, conn)
    except db.NoVersionError:
        raise HTTPException(status_code=404, detail=f"版本 {version_id} 不存在")


@app.post(
    "/generations",
    response_model=GenerationOut,
    status_code=202,
    tags=["生成任务"],
)
def create_generation(conn: Connection = Depends(get_db)):
    """开始一次生成：同步固定当前最新 old/new 版本快照，计算在后台串行执行。

    快照在本请求的事务内确定；此后替换任一输入不影响本任务。
    """
    try:
        with conn:
            gen_id = db.create_generation(conn)
    except db.NoVersionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    assert _executor is not None
    _executor.submit(db.run_generation, gen_id, DB_PATH)
    return db.get_generation(gen_id, conn)


@app.get("/generations", response_model=list[GenerationOut], tags=["生成任务"])
def list_generations(conn: Connection = Depends(get_db)):
    return db.list_generations(conn)


@app.get("/generations/{gen_id}", response_model=GenerationOut, tags=["生成任务"])
def get_generation(
    gen_id: int = PathParam(..., ge=1), conn: Connection = Depends(get_db)
):
    row = db.get_generation(gen_id, conn)
    if row is None:
        raise HTTPException(status_code=404, detail=f"生成任务 {gen_id} 不存在")
    return row


@app.get("/result", response_model=CurrentResultOut, tags=["发布结果"])
def get_current_result(conn: Connection = Depends(get_db)):
    """当前已发布结果（片区汇总 + 两份输入版本）。

    新结果完整发布前，这里始终返回上一份已发布结果；从未发布过时返回 404。
    """
    result = db.get_current_result(conn)
    if result is None:
        raise HTTPException(status_code=404, detail="尚无已发布的生成结果")
    return result


def _verification_items(payload: VerifiedSubmission) -> list[dict]:
    return [{"id": it.id, "households": it.households} for it in payload.items]


@app.post(
    "/verifications",
    response_model=VerificationOut,
    status_code=201,
    tags=["核定供数"],
    responses={
        422: {"model": InfeasibleVerificationOut, "description": "总量相同但不相容"},
        400: {"description": "编号集合或总户数与当前任务不符"},
        409: {"description": "尚无已发布的生成任务"},
    },
)
def submit_verification(
    payload: VerifiedSubmission, conn: Connection = Depends(get_db)
):
    """整版提交每个片区的外部核定户数（非负整数）。

    核定记录绑定读取时刻的**当前已发布生成任务**及其两份输入版本：
    - 201：相容，已保存逐格整数流向（两侧合计分别吻合），并成为当前有效核定；
    - 422：总量相同但无法只沿面积相交关系整数分摊，返回受限旧格、可达片区与缺额，
      失败记录留痕但**不覆盖**旧有效核定；
    - 400：片区编号集合或核定总户数与当前任务不符；
    - 409：尚无已发布的生成任务。
    """
    try:
        with conn:
            status_code, body = db.create_verification(
                _verification_items(payload), conn
            )
    except db.NoPublishedResultError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except db.ValidationError as exc:
        # 编号/总量不符：事务回滚，不产生任何核定记录
        raise HTTPException(status_code=400, detail=str(exc))

    if status_code == 422:
        # 事务已提交（infeasible 记录已留痕），用结构化诊断返回 422
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "核定总户数与旧网格总户数相同，但旧格户数无法只沿面积相交的片区"
                    "整数分摊到各片区核定值"
                ),
                "verification_id": body["id"],
                "generation_id": body["generation_id"],
                "old_version": body["old_version"],
                "new_version": body["new_version"],
                "total_households": body["total_households"],
                "blocked_old_ids": body["blocked_old_ids"],
                "reachable_district_ids": body["reachable_district_ids"],
                "shortfall": body["shortfall"],
            },
        )
    return body


@app.get(
    "/verifications/current",
    response_model=VerificationOut,
    tags=["核定供数"],
)
def get_current_verification(conn: Connection = Depends(get_db)):
    """与**当前已发布任务**对应的有效核定。

    从未有过有效核定、或发布任务已切换而新任务尚无核定时返回 404；
    切换前的旧核定不会被当成新任务的结果。
    """
    verification = db.get_current_verification(conn)
    if verification is None:
        raise HTTPException(
            status_code=404,
            detail="当前已发布任务尚无有效的核定供数",
        )
    return verification


@app.get(
    "/verifications/{verification_id}",
    response_model=VerificationOut,
    tags=["核定供数"],
)
def get_verification(
    verification_id: int = PathParam(..., ge=1),
    conn: Connection = Depends(get_db),
):
    """按编号查询单次核定提交（含失败记录，便于追溯诊断）。"""
    verification = db.get_verification(verification_id, conn)
    if verification is None:
        raise HTTPException(
            status_code=404, detail=f"核定记录 {verification_id} 不存在"
        )
    return verification


@app.get("/verifications", response_model=list[VerificationOut], tags=["核定供数"])
def list_verifications(conn: Connection = Depends(get_db)):
    """列出全部核定提交（按提交先后，含 infeasible 留痕记录）。"""
    return db.list_verifications(conn)
