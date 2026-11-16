"""SQLite 持久化层。

设计要点：
- 输入按“整版提交”存为不可变版本（versions + cells），替换即产生新版本。
- 生成任务在创建（生成开始）的同一事务内固定 old/new 版本号，
  之后即使输入被替换，任务仍按该快照计算。
- current_result 为单行表，只在新结果完整计算成功后于同一事务内切换，
  失败或进行中绝不触碰它，旧发布结果保持可查。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import allocation as alloc
from . import flow as flowmod
from .config import DB_PATH, MAX_ITEMS_PER_SUBMISSION

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('old', 'new')),
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cells (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL REFERENCES versions(id),
    seq          INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    x1           INTEGER NOT NULL,
    y1           INTEGER NOT NULL,
    x2           INTEGER NOT NULL,
    y2           INTEGER NOT NULL,
    area         INTEGER NOT NULL,
    households   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cells_version ON cells(version_id);

CREATE TABLE IF NOT EXISTS generations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    old_version   INTEGER REFERENCES versions(id),
    new_version   INTEGER REFERENCES versions(id),
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS result_parts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    district_id    TEXT NOT NULL,
    old_id         TEXT NOT NULL,
    overlap_area   INTEGER NOT NULL,
    households     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parts_generation ON result_parts(generation_id);

-- 单行表：始终指向“当前已发布结果”
CREATE TABLE IF NOT EXISTS current_result (
    only_row       INTEGER PRIMARY KEY CHECK (only_row = 1),
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    published_at   TEXT NOT NULL,
    total_households INTEGER NOT NULL
);

-- 外部核定供数：每次整版提交一行，绑定读取时的生成任务及两份输入版本。
-- 失败记录也保留（用于审计与解释），但只有 feasible 记录会被 current_verification 引用。
CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id  INTEGER NOT NULL REFERENCES generations(id),
    old_version    INTEGER NOT NULL REFERENCES versions(id),
    new_version    INTEGER NOT NULL REFERENCES versions(id),
    status         TEXT NOT NULL CHECK (status IN ('feasible', 'infeasible')),
    total_households INTEGER NOT NULL,
    shortfall      INTEGER,
    submitted_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_generation ON verifications(generation_id);

-- 整版提交的片区核定户数（失败记录同样保留）
CREATE TABLE IF NOT EXISTS verification_demands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL REFERENCES verifications(id),
    seq             INTEGER NOT NULL,
    district_id     TEXT NOT NULL,
    households      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_demands_verification ON verification_demands(verification_id);

-- 相容时保存的逐格整数流向
CREATE TABLE IF NOT EXISTS verification_flows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL REFERENCES verifications(id),
    seq             INTEGER NOT NULL,
    old_id          TEXT NOT NULL,
    district_id     TEXT NOT NULL,
    households      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flows_verification ON verification_flows(verification_id);

-- 不可行诊断：受限旧格 / 可达片区（按 seq 保持确定顺序）
CREATE TABLE IF NOT EXISTS verification_blocked (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL REFERENCES verifications(id),
    kind            TEXT NOT NULL CHECK (kind IN ('old', 'district')),
    seq             INTEGER NOT NULL,
    ref_id          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocked_verification ON verification_blocked(verification_id);

-- 单行表：始终指向“当前有效核定”，且该核定必须属于当前已发布的生成任务
CREATE TABLE IF NOT EXISTS current_verification (
    only_row        INTEGER PRIMARY KEY CHECK (only_row = 1),
    verification_id INTEGER NOT NULL REFERENCES verifications(id),
    generation_id   INTEGER NOT NULL REFERENCES generations(id),
    published_at    TEXT NOT NULL
);
"""


class NoVersionError(Exception):
    """缺少某一类输入的已提交版本。"""


class ValidationError(Exception):
    """输入业务校验失败（编号重复、内部重叠等）。"""


class NoPublishedResultError(Exception):
    """尚无已发布的生成结果，无法进行核定供数。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    # 保证任何连接（含后台工作线程）打开时表结构都已就绪（DDL 幂等）
    conn.executescript(SCHEMA)
    return conn


def init_db(db_path: Path | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- 输入版本 ----------

def _validate_items(kind: str, items: list[dict[str, Any]]) -> None:
    """业务校验：数量上限、编号唯一、同版矩形互不重叠。"""
    if not items:
        raise ValidationError("提交内容不能为空")
    if len(items) > MAX_ITEMS_PER_SUBMISSION:
        raise ValidationError(
            f"每版最多 {MAX_ITEMS_PER_SUBMISSION} 个条目，收到 {len(items)} 个"
        )
    ids = [it["id"] for it in items]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValidationError(f"条目编号必须唯一，重复编号：{', '.join(dupes)}")
    rects = [(it["id"], it["rect"]) for it in items]
    try:
        alloc.assert_no_internal_overlap(rects)  # 抛 ValueError
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def insert_version(kind: str, items: list[dict[str, Any]], conn: sqlite3.Connection) -> int:
    """在给定连接/事务内写入一个不可变版本，返回版本号。"""
    _validate_items(kind, items)
    cur = conn.execute(
        "INSERT INTO versions(kind, created_at, item_count) VALUES (?, ?, ?)",
        (kind, _now(), len(items)),
    )
    version_id = cur.lastrowid
    for seq, it in enumerate(items):
        x1, y1, x2, y2 = it["rect"]
        conn.execute(
            "INSERT INTO cells(version_id, seq, cell_id, x1, y1, x2, y2, area, households)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                seq,
                it["id"],
                x1,
                y1,
                x2,
                y2,
                alloc.rect_area(it["rect"]),
                it.get("households"),
            ),
        )
    return version_id


def latest_version_id(kind: str, conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT MAX(id) AS m FROM versions WHERE kind = ?", (kind,)
    ).fetchone()
    return row["m"]


def _load_version(conn: sqlite3.Connection, version_id: int) -> dict[str, Any]:
    vrow = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    if vrow is None:
        raise NoVersionError(f"版本 {version_id} 不存在")
    cells = conn.execute(
        "SELECT * FROM cells WHERE version_id = ? ORDER BY seq", (version_id,)
    ).fetchall()
    return {
        "version": vrow["id"],
        "kind": vrow["kind"],
        "created_at": vrow["created_at"],
        "item_count": vrow["item_count"],
        "items": [
            {
                "id": c["cell_id"],
                "x1": c["x1"],
                "y1": c["y1"],
                "x2": c["x2"],
                "y2": c["y2"],
                "area": c["area"],
                "households": c["households"],
            }
            for c in cells
        ],
    }


def get_version(version_id: int, conn: sqlite3.Connection) -> dict[str, Any]:
    return _load_version(conn, version_id)


def list_versions(kind: str | None, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute(
            "SELECT id FROM versions WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM versions ORDER BY id").fetchall()
    return [_load_version(conn, r["id"]) for r in rows]


# ---------- 生成任务 ----------

def create_generation(conn: sqlite3.Connection) -> int:
    """创建生成任务并在同一事务固定当前最新 old/new 版本快照。"""
    old_v = latest_version_id("old", conn)
    new_v = latest_version_id("new", conn)
    missing = []
    if old_v is None:
        missing.append("旧网格")
    if new_v is None:
        missing.append("新片区")
    if missing:
        raise NoVersionError("缺少已提交的" + "、".join(missing) + "版本，无法生成")
    cur = conn.execute(
        "INSERT INTO generations(status, old_version, new_version, created_at)"
        " VALUES ('pending', ?, ?, ?)",
        (old_v, new_v, _now()),
    )
    return cur.lastrowid


def _load_snapshot(conn: sqlite3.Connection, gen_id: int) -> dict[str, Any]:
    grow = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    old_version = _load_version(conn, grow["old_version"])
    new_version = _load_version(conn, grow["new_version"])
    old_cells = [
        alloc.OldCell(
            id=c["id"],
            rect=(c["x1"], c["y1"], c["x2"], c["y2"]),
            households=c["households"],
        )
        for c in old_version["items"]
    ]
    districts = [
        alloc.NewDistrict(
            id=c["id"], rect=(c["x1"], c["y1"], c["x2"], c["y2"])
        )
        for c in new_version["items"]
    ]
    return {
        "row": grow,
        "old_version": old_version,
        "new_version": new_version,
        "old_cells": old_cells,
        "districts": districts,
    }


def run_generation(gen_id: int, db_path: Path | None = None) -> None:
    """工作线程入口：按任务固定的快照计算并原子发布；失败只标记任务。

    任何异常都会把任务落为 failed（而不是永久 running），
    且失败路径绝不写入 current_result，旧发布结果保持可查。
    """
    conn = get_conn(db_path)
    try:
        # 1. 抢占任务并读取快照（任务版本号在创建时已固定，不受新提交影响）
        conn.execute(
            "UPDATE generations SET status='running', started_at=? "
            "WHERE id=? AND status='pending'",
            (_now(), gen_id),
        )
        conn.commit()
        snap = _load_snapshot(conn, gen_id)

        # 2. 纯函数计算（覆盖校验 + 最大余数分摊）
        try:
            allocations = alloc.allocate(snap["old_cells"], snap["districts"])
        except ValueError as exc:
            _mark_failed(conn, gen_id, str(exc))
            return

        total = sum(c.households for c in snap["old_cells"])
        allocated_total = sum(a.households for a in allocations)
        if allocated_total != total:
            # 理论上不会发生；作为守恒护栏，宁可失败也不发布错误结果
            _mark_failed(
                conn,
                gen_id,
                f"守恒校验失败：原总户数 {total}，分摊合计 {allocated_total}",
            )
            return

        # 3. 原子发布：明细 + 任务成功 + 切换当前结果，同一事务
        # 事务异常自动回滚，current_result 保持旧值
        with conn:
            for a in allocations:
                conn.execute(
                    "INSERT INTO result_parts(generation_id, district_id, old_id,"
                    " overlap_area, households) VALUES (?, ?, ?, ?, ?)",
                    (gen_id, a.district_id, a.old_id, a.overlap_area, a.households),
                )
            conn.execute(
                "UPDATE generations SET status='succeeded', error=NULL, finished_at=? WHERE id=?",
                (_now(), gen_id),
            )
            conn.execute(
                "INSERT INTO current_result(only_row, generation_id, published_at,"
                " total_households) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(only_row) DO UPDATE SET "
                "generation_id=excluded.generation_id,"
                " published_at=excluded.published_at,"
                " total_households=excluded.total_households",
                (gen_id, _now(), total),
            )
    except Exception as exc:  # 兜底：任何意外都落 failed，不卡死任务
        try:
            _mark_failed(conn, gen_id, f"生成失败：{type(exc).__name__}: {exc}")
        except sqlite3.Error:
            pass
    finally:
        conn.close()


def _mark_failed(conn: sqlite3.Connection, gen_id: int, message: str) -> None:
    conn.execute(
        "UPDATE generations SET status='failed', error=?, finished_at=? WHERE id=?",
        (message, _now(), gen_id),
    )
    conn.commit()


def get_generation(gen_id: int, conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    return dict(row) if row else None


def list_generations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM generations ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_current_result(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """组装当前发布结果：片区汇总 + 两份输入版本。无发布时返回 None。"""
    cur = conn.execute("SELECT * FROM current_result WHERE only_row=1").fetchone()
    if cur is None:
        return None
    gen_id = cur["generation_id"]
    grow = conn.execute("SELECT * FROM generations WHERE id=?", (gen_id,)).fetchone()
    old_version = _load_version(conn, grow["old_version"])
    new_version = _load_version(conn, grow["new_version"])

    parts = conn.execute(
        "SELECT * FROM result_parts WHERE generation_id=? ORDER BY id", (gen_id,)
    ).fetchall()
    by_district: dict[str, list[dict[str, Any]]] = {
        c["id"]: [] for c in new_version["items"]
    }
    for p in parts:
        by_district.setdefault(p["district_id"], []).append(
            {
                "district_id": p["district_id"],
                "old_id": p["old_id"],
                "overlap_area": p["overlap_area"],
                "households": p["households"],
            }
        )
    districts = []
    for d_id in sorted(by_district, key=alloc._id_sort_key):
        detail = sorted(by_district[d_id], key=lambda a: alloc._id_sort_key(a["old_id"]))
        districts.append(
            {
                "district_id": d_id,
                "allocated_households": sum(a["households"] for a in detail),
                "detail": detail,
            }
        )
    return {
        "generation_id": gen_id,
        "published_at": cur["published_at"],
        "total_households": cur["total_households"],
        "old_version": old_version,
        "new_version": new_version,
        "districts": districts,
    }


# ---------- 外部核定供数 ----------

def _current_published_generation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM current_result WHERE only_row=1").fetchone()
    if cur is None:
        return None
    return conn.execute(
        "SELECT * FROM generations WHERE id=?", (cur["generation_id"],)
    ).fetchone()


def create_verification(
    items: list[dict[str, Any]], conn: sqlite3.Connection
) -> tuple[int, dict[str, Any]]:
    """整版提交片区核定户数，判定与当前已发布任务的相容性。

    绑定读取时刻的生成任务（及其 old/new 版本快照）。
    - 编号集合或总量不符：抛 ValidationError，不写入任何核定记录；
    - 总量相同但不可行：保存一条 infeasible 记录（含受限旧格/可达片区/缺额），
      不触碰 current_verification，旧有效核定保持不变；
    - 相容：保存 feasible 记录与逐格整数流向，并在同一事务切换 current_verification。

    返回 ("http_status", payload)，由路由层决定如何抛出，保证本函数的事务先提交。
    """
    grow = _current_published_generation(conn)
    if grow is None:
        raise NoPublishedResultError("尚无已发布的生成结果，无法核定供数")

    gen_id = grow["id"]
    old_version = _load_version(conn, grow["old_version"])
    new_version = _load_version(conn, grow["new_version"])

    # 1. 编号集合必须与当前任务的片区编号完全一致
    expected_ids = [c["id"] for c in new_version["items"]]
    submitted_ids = [it["id"] for it in items]
    missing = [i for i in expected_ids if i not in set(submitted_ids)]
    unknown = [i for i in submitted_ids if i not in set(expected_ids)]
    if missing or unknown:
        details = []
        if missing:
            details.append("缺少片区编号：" + ", ".join(missing))
        if unknown:
            details.append("未知片区编号：" + ", ".join(unknown))
        raise ValidationError(
            "核定编号与当前已发布任务（生成任务 "
            f"{gen_id}）的片区编号不符；" + "；".join(details)
        )

    demand_by_id = {it["id"]: it["households"] for it in items}
    total_verified = sum(demand_by_id.values())
    total_old = sum(c["households"] for c in old_version["items"])
    if total_verified != total_old:
        raise ValidationError(
            f"核定总户数 {total_verified} 与当前任务旧网格总户数 {total_old} 不一致，"
            "无法判定相容（两侧总量必须相同）"
        )

    # 2. 依据任务快照的几何关系构造相交边（只看是否面积相交，不沿用面积比例）
    old_rects = [
        alloc.OldCell(
            id=c["id"],
            rect=(c["x1"], c["y1"], c["x2"], c["y2"]),
            households=c["households"],
        )
        for c in old_version["items"]
    ]
    district_rects = [
        alloc.NewDistrict(
            id=c["id"], rect=(c["x1"], c["y1"], c["x2"], c["y2"])
        )
        for c in new_version["items"]
    ]
    overlap_edges = [
        (cell.id, d.id)
        for cell in old_rects
        for d in district_rects
        if alloc.overlap_area(cell.rect, d.rect) > 0
    ]

    result = flowmod.solve_transport(
        old_cells=[(c.id, c.households) for c in old_rects],
        districts=[(d.id, demand_by_id[d.id]) for d in district_rects],
        overlap_edges=overlap_edges,
    )

    # 3. 写入核定主记录（成功/失败都留痕）
    cur = conn.execute(
        "INSERT INTO verifications(generation_id, old_version, new_version, status,"
        " total_households, shortfall, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            gen_id,
            old_version["version"],
            new_version["version"],
            "feasible" if result.feasible else "infeasible",
            total_verified,
            None if result.feasible else result.shortfall,
            _now(),
        ),
    )
    verification_id = cur.lastrowid

    for seq, it in enumerate(items):
        conn.execute(
            "INSERT INTO verification_demands(verification_id, seq, district_id, households)"
            " VALUES (?, ?, ?, ?)",
            (verification_id, seq, it["id"], it["households"]),
        )

    if result.feasible:
        for seq, f in enumerate(result.flows):
            conn.execute(
                "INSERT INTO verification_flows(verification_id, seq, old_id, district_id,"
                " households) VALUES (?, ?, ?, ?, ?)",
                (verification_id, seq, f.old_id, f.district_id, f.households),
            )
        # 有效核定原子切换；失败路径绝不执行此语句，旧核定保持有效
        conn.execute(
            "INSERT INTO current_verification(only_row, verification_id, generation_id,"
            " published_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(only_row) DO UPDATE SET "
            "verification_id=excluded.verification_id,"
            " generation_id=excluded.generation_id,"
            " published_at=excluded.published_at",
            (verification_id, gen_id, _now()),
        )
    else:
        for seq, oid in enumerate(result.restricted_old_ids):
            conn.execute(
                "INSERT INTO verification_blocked(verification_id, kind, seq, ref_id)"
                " VALUES (?, 'old', ?, ?)",
                (verification_id, seq, oid),
            )
        for seq, did in enumerate(result.reachable_district_ids):
            conn.execute(
                "INSERT INTO verification_blocked(verification_id, kind, seq, ref_id)"
                " VALUES (?, 'district', ?, ?)",
                (verification_id, seq, did),
            )

    payload = _load_verification(conn, verification_id)
    if result.feasible:
        return 201, payload
    return 422, payload


def _load_verification(
    conn: sqlite3.Connection, verification_id: int
) -> dict[str, Any] | None:
    vrow = conn.execute(
        "SELECT * FROM verifications WHERE id=?", (verification_id,)
    ).fetchone()
    if vrow is None:
        return None

    demands = conn.execute(
        "SELECT district_id, households FROM verification_demands"
        " WHERE verification_id=? ORDER BY seq",
        (verification_id,),
    ).fetchall()
    flows = conn.execute(
        "SELECT old_id, district_id, households FROM verification_flows"
        " WHERE verification_id=? ORDER BY seq",
        (verification_id,),
    ).fetchall()
    blocked = conn.execute(
        "SELECT kind, ref_id FROM verification_blocked"
        " WHERE verification_id=? ORDER BY kind, seq",
        (verification_id,),
    ).fetchall()

    old_totals: dict[str, int] = {}
    flow_district_totals: dict[str, int] = {}
    for f in flows:
        old_totals[f["old_id"]] = old_totals.get(f["old_id"], 0) + f["households"]
        flow_district_totals[f["district_id"]] = (
            flow_district_totals.get(f["district_id"], 0) + f["households"]
        )
    # 相容时按流向汇总的片区合计与外部核定必然相等；保留 demands 原值作为对外视图
    district_totals: dict[str, int] = {d["district_id"]: d["households"] for d in demands}
    if vrow["status"] == "feasible":
        # 零户数旧格/零需求片区没有正流量边，按绑定版本补齐零值后再做双侧护栏
        old_version = _load_version(conn, vrow["old_version"])
        for c in old_version["items"]:
            old_totals.setdefault(c["id"], 0)
        for did in district_totals:
            flow_district_totals.setdefault(did, 0)
        # 守恒护栏：逐片区流向合计必须等于核定户数
        assert flow_district_totals == district_totals

    return {
        "id": vrow["id"],
        "generation_id": vrow["generation_id"],
        "old_version": vrow["old_version"],
        "new_version": vrow["new_version"],
        "status": vrow["status"],
        "total_households": vrow["total_households"],
        "submitted_at": vrow["submitted_at"],
        "demands": [dict(d) for d in demands],
        "flows": [dict(f) for f in flows],
        "blocked_old_ids": [b["ref_id"] for b in blocked if b["kind"] == "old"],
        "reachable_district_ids": [
            b["ref_id"] for b in blocked if b["kind"] == "district"
        ],
        "shortfall": vrow["shortfall"],
        "old_totals": [
            {"old_id": k, "households": old_totals[k]} for k in sorted(old_totals)
        ],
        "district_totals": [
            {"district_id": k, "households": district_totals[k]}
            for k in sorted(district_totals)
        ],
    }


def get_verification(
    verification_id: int, conn: sqlite3.Connection
) -> dict[str, Any] | None:
    return _load_verification(conn, verification_id)


def list_verifications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM verifications ORDER BY id").fetchall()
    return [_load_verification(conn, r["id"]) for r in rows]


def get_current_verification(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """返回与当前已发布任务对应的有效核定。

    仅当 current_verification 指向的核定所属任务仍是当前发布任务时返回；
    发布切换后旧核定不会被当成新任务的结果（返回 None）。
    """
    cur = conn.execute(
        "SELECT * FROM current_verification WHERE only_row=1"
    ).fetchone()
    if cur is None:
        return None
    published = _current_published_generation(conn)
    if published is None or cur["generation_id"] != published["id"]:
        return None
    return _load_verification(conn, cur["verification_id"])
