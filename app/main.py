"""实验室混样批次追溯 API。

- POST /samples                        登记原始样本
- POST /pools                          创建混样批次（引用已有批次 + 正整数份数；命中禁配规则返回 409）
- POST /pool-plans                     按目标占比反算整数份数配方（只规划，不创建批次）
- POST /exclusion-rules                登记原始样本禁配对（无序对，重复登记不生成第二条）
- GET  /exclusion-rules                列出禁配规则
- GET  /batches                        列出全部批次
- GET  /batches/{batch_id}             批次详情（混样含配方）
- GET  /batches/{batch_id}/composition 递归计算原始样本占比（约分后的分子/分母）
- POST /batches/{batch_id}/recipe-preview 配方变更影响预演（内存计算，不写库）
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from fractions import Fraction

from fastapi import Depends, FastAPI, HTTPException, Response, status

from . import db
from .schemas import (
    AffectedBatchOut,
    BatchOut,
    ComponentOut,
    CompositionOut,
    ExclusionRuleCreate,
    ExclusionRuleOut,
    FractionOut,
    PoolCreate,
    PoolPlanOut,
    PoolPlanRecipeItemOut,
    PoolPlanRequest,
    PreviewComponentOut,
    RecipeItemOut,
    RecipePreviewOut,
    RecipePreviewRequest,
    SampleCreate,
)
from .service import (
    PoolPlanError,
    load_graph,
    batch_composition,
    find_exclusion_conflict,
    pool_plan,
    reachable_original_samples,
    recipe_preview,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="实验室混样批次追溯 API",
    version="1.0.0",
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _fetch_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _batch_out(conn: sqlite3.Connection, row: sqlite3.Row) -> BatchOut:
    recipe = None
    if row["kind"] == "pool":
        recipe = [
            RecipeItemOut(
                batch_id=r["ingredient_id"],
                batch_name=r["name"],
                portions=r["portions"],
            )
            for r in conn.execute(
                """
                SELECT pr.ingredient_id, b.name, pr.portions
                FROM pool_recipes pr
                JOIN batches b ON b.id = pr.ingredient_id
                WHERE pr.pool_id = ?
                ORDER BY pr.ingredient_id
                """,
                (row["id"],),
            )
        ]
    return BatchOut(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        created_at=row["created_at"],
        recipe=recipe,
    )


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/samples",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="登记原始样本",
)
def create_sample(payload: SampleCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    try:
        cur = conn.execute(
            "INSERT INTO batches (name, kind) VALUES (?, 'sample')", (payload.name,)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, cur.lastrowid))


@app.post(
    "/pools",
    response_model=BatchOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建混样批次",
)
def create_pool(payload: PoolCreate, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    # 同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)

    # 检查与写入在同一个 IMMEDIATE 写事务内完成：并发登记的新规则要么已在本
    # 事务的读取范围内，要么在本事务提交后才生效，不会出现“规则已生效却漏检”
    try:
        with db.write_tx(conn):
            found = {
                row["id"]: row
                for row in conn.execute(
                    f"SELECT id, kind FROM batches WHERE id IN ({placeholders})", ids
                )
            }
            missing = [i for i in ids if i not in found]
            if missing:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
                )

            # 沿配方图递归求所有原料可达的原始样本（含嵌套混样、多路径汇合）
            kinds, recipes = load_graph(conn)
            reachable = reachable_original_samples(kinds, recipes, ids)

            # 命中任一禁配对：返回按 id 升序的冲突样本对，不创建批次或配方
            conflict = find_exclusion_conflict(conn, reachable)
            if conflict is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail={
                        "message": "混样命中原始样本禁配规则",
                        "conflicting_sample_ids": [conflict[0], conflict[1]],
                    },
                )

            cur = conn.execute(
                "INSERT INTO batches (name, kind) VALUES (?, 'pool')", (payload.name,)
            )
            pool_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO pool_recipes (pool_id, ingredient_id, portions) VALUES (?, ?, ?)",
                [(pool_id, i, merged[i]) for i in ids],
            )
    except sqlite3.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail=f"批次名称已存在: {payload.name}"
        ) from None
    return _batch_out(conn, _fetch_batch(conn, pool_id))


def _exclusion_rule_out(conn: sqlite3.Connection, row: sqlite3.Row) -> ExclusionRuleOut:
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (?, ?)",
            (row["sample_a_id"], row["sample_b_id"]),
        )
    }
    return ExclusionRuleOut(
        id=row["id"],
        sample_a_id=row["sample_a_id"],
        sample_b_id=row["sample_b_id"],
        sample_a_name=names[row["sample_a_id"]],
        sample_b_name=names[row["sample_b_id"]],
        created_at=row["created_at"],
    )


@app.post(
    "/exclusion-rules",
    response_model=ExclusionRuleOut,
    summary="登记原始样本禁配对",
)
def create_exclusion_rule(
    payload: ExclusionRuleCreate,
    response: Response,
    conn: sqlite3.Connection = Depends(get_db),
) -> ExclusionRuleOut:
    # 无序对按 id 升序规范化；两个 id 相同已由 schema 校验拒绝（422）
    a_id, b_id = sorted((payload.sample_a_id, payload.sample_b_id))

    # 登记与混样创建共用同一种 IMMEDIATE 写事务
    with db.write_tx(conn):
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, name, kind FROM batches WHERE id IN (?, ?)", (a_id, b_id)
            )
        }
        missing = [i for i in (a_id, b_id) if i not in rows]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
            )
        non_samples = [i for i in (a_id, b_id) if rows[i]["kind"] != "sample"]
        if non_samples:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"禁配对象必须是原始样本（kind='sample'）: {non_samples}",
            )

        # 重复登记同一无序对不生成第二条：INSERT OR IGNORE 命中 UNIQUE 时跳过
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO exclusion_rules (sample_a_id, sample_b_id)
            VALUES (?, ?)
            """,
            (a_id, b_id),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT * FROM exclusion_rules WHERE sample_a_id = ? AND sample_b_id = ?",
                (a_id, b_id),
            ).fetchone()
            response.status_code = status.HTTP_200_OK
        else:
            row = conn.execute(
                "SELECT * FROM exclusion_rules WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            response.status_code = status.HTTP_201_CREATED
        return _exclusion_rule_out(conn, row)


@app.get(
    "/exclusion-rules",
    response_model=list[ExclusionRuleOut],
    summary="列出原始样本禁配规则",
)
def list_exclusion_rules(conn: sqlite3.Connection = Depends(get_db)) -> list[ExclusionRuleOut]:
    rows = conn.execute(
        "SELECT * FROM exclusion_rules ORDER BY sample_a_id, sample_b_id, id"
    ).fetchall()
    return [_exclusion_rule_out(conn, row) for row in rows]


@app.post(
    "/pool-plans",
    response_model=PoolPlanOut,
    summary="按目标占比规划混样配方（不创建批次）",
)
def plan_pool(
    payload: PoolPlanRequest, conn: sqlite3.Connection = Depends(get_db)
) -> PoolPlanOut:
    # 两个原料与目标样本都必须是已存在的批次；未知批次沿用 /pools 的 400 校验
    ids = [payload.ingredient_a_batch_id, payload.ingredient_b_batch_id, payload.sample_id]
    placeholders = ", ".join("?" for _ in ids)
    rows = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT id, name, kind FROM batches WHERE id IN ({placeholders})", ids
        )
    }
    missing = [i for i in ids if i not in rows]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}")
    if rows[payload.sample_id]["kind"] != "sample":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"目标样本必须是原始样本（kind='sample'）: {payload.sample_id}",
        )

    target = Fraction(payload.numerator, payload.denominator)
    try:
        portions_a, portions_b, composition = pool_plan(
            conn,
            payload.ingredient_a_batch_id,
            payload.ingredient_b_batch_id,
            payload.sample_id,
            target,
            payload.max_total_portions,
        )
    except PoolPlanError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None

    # 新混样涉及的原始样本名称一次性解析
    sample_ids = sorted(composition)
    placeholders = ", ".join("?" for _ in sample_ids)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            f"SELECT id, name FROM batches WHERE id IN ({placeholders})", sample_ids
        )
    }
    components = [
        ComponentOut(
            sample_id=sid,
            sample_name=names[sid],
            numerator=composition[sid].numerator,
            denominator=composition[sid].denominator,
        )
        for sid in sample_ids
    ]

    return PoolPlanOut(
        sample_id=payload.sample_id,
        target=FractionOut(numerator=target.numerator, denominator=target.denominator),
        total_portions=portions_a + portions_b,
        recipe=[
            PoolPlanRecipeItemOut(
                batch_id=payload.ingredient_a_batch_id, portions=portions_a
            ),
            PoolPlanRecipeItemOut(
                batch_id=payload.ingredient_b_batch_id, portions=portions_b
            ),
        ],
        components=components,
    )


@app.get("/batches", response_model=list[BatchOut], summary="列出全部批次")
def list_batches(conn: sqlite3.Connection = Depends(get_db)) -> list[BatchOut]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [_batch_out(conn, row) for row in rows]


@app.get("/batches/{batch_id}", response_model=BatchOut, summary="批次详情")
def get_batch(batch_id: int, conn: sqlite3.Connection = Depends(get_db)) -> BatchOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    return _batch_out(conn, row)


@app.get(
    "/batches/{batch_id}/composition",
    response_model=CompositionOut,
    summary="递归计算原始样本占比",
)
def get_composition(
    batch_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> CompositionOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")

    composition = batch_composition(conn, batch_id)
    names = {
        r["id"]: r["name"]
        for r in conn.execute(
            "SELECT id, name FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in composition),
            list(composition),
        )
    }
    components = [
        ComponentOut(
            sample_id=sample_id,
            sample_name=names[sample_id],
            numerator=frac.numerator,
            denominator=frac.denominator,
        )
        for sample_id, frac in sorted(composition.items())
    ]
    return CompositionOut(
        batch_id=row["id"],
        batch_name=row["name"],
        kind=row["kind"],
        components=components,
    )


@app.post(
    "/batches/{batch_id}/recipe-preview",
    response_model=RecipePreviewOut,
    summary="配方变更影响预演（不修改数据库）",
)
def preview_recipe(
    batch_id: int,
    payload: RecipePreviewRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> RecipePreviewOut:
    row = _fetch_batch(conn, batch_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"批次不存在: {batch_id}")
    if row["kind"] != "pool":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原始样本没有配方可替换: {batch_id}",
        )

    # 与创建混样一致：同一批次在配方中重复出现时，份数合并
    merged: dict[int, int] = {}
    for item in payload.recipe:
        merged[item.batch_id] = merged.get(item.batch_id, 0) + item.portions

    ids = sorted(merged)
    placeholders = ", ".join("?" for _ in ids)
    found = {
        r["id"]
        for r in conn.execute(f"SELECT id FROM batches WHERE id IN ({placeholders})", ids)
    }
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"未知批次 id: {missing}"
        )

    # 避免预演成环：原料只能引用比目标批次更早创建（id 更小）的批次
    too_late = [i for i in ids if i >= batch_id]
    if too_late:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"原料只能引用比批次 {batch_id} 更早创建的批次: {too_late}",
        )

    affected = recipe_preview(conn, batch_id, merged)

    # 一次性解析受影响批次、涉及样本与替代配方原料的名称/类型
    involved = set(affected) | set(ids)
    for original, preview in affected.values():
        involved.update(original)
        involved.update(preview)
    info = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, kind FROM batches WHERE id IN (%s)"
            % ", ".join("?" for _ in involved),
            sorted(involved),
        )
    }

    def frac_out(frac: Fraction) -> FractionOut:
        return FractionOut(numerator=frac.numerator, denominator=frac.denominator)

    batches_out = []
    for bid, (original, preview) in affected.items():
        components = [
            PreviewComponentOut(
                sample_id=sample_id,
                sample_name=info[sample_id]["name"],
                original=frac_out(original.get(sample_id, Fraction(0))),
                preview=frac_out(preview.get(sample_id, Fraction(0))),
                delta=frac_out(
                    preview.get(sample_id, Fraction(0))
                    - original.get(sample_id, Fraction(0))
                ),
            )
            for sample_id in sorted(set(original) | set(preview))
        ]
        batches_out.append(
            AffectedBatchOut(
                batch_id=bid,
                batch_name=info[bid]["name"],
                kind=info[bid]["kind"],
                components=components,
            )
        )

    return RecipePreviewOut(
        batch_id=row["id"],
        batch_name=row["name"],
        recipe=[
            RecipeItemOut(batch_id=i, batch_name=info[i]["name"], portions=merged[i])
            for i in ids
        ],
        affected_batches=batches_out,
    )
