# 实验室混样批次追溯 API

基于 **FastAPI + SQLite** 的实验室混样（pooling）批次追溯服务：登记原始样本、按配方创建混样批次，并对任一批次递归追溯其中各原始样本的精确占比（约分后的分子/分母，无浮点误差）。

## 功能特性

- 登记**原始样本**、按配方创建**混样批次**（配方 = 已有批次 + 各自的正整数投入份数）
- 创建校验：拒绝**空配方**（422）、**未知批次**（400）、**非正整数份数**（422，含 0、负数、小数、字符串）
- 新批次只能引用**已创建**的批次，引用关系天然构成有向无环图，不可能成环
- 占比追溯：按每层“投入份数 / 该层总份数”递归分摊；同一原始样本经多条路径到达时**自动合并**；基于 `fractions.Fraction` 精确有理数运算，返回**约分后**的分子与分母
- **目标占比规划**：给定两个不同的已有批次、一个原始样本与目标占比，反算使新混样精确达标的**正整数投入份数**，只返回总份数最小的整数解（两原料占比都等于目标时取 1:1），配方可直接提交 `/pools`；**只规划、不创建批次**
- **配方变更影响预演**：提交完整替代配方，在内存中演算目标批次及全部下游批次各原始样本的原占比、预演占比与有符号差值，**不修改数据库**
- 依赖安装与数据库建表全部纳入启动流程，无需手工初始化

## 快速开始（Docker）

唯一需要的一条命令：

```bash
docker compose up --build
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000> | API 服务 |
| <http://localhost:8000/docs> | Swagger 交互式文档（可直接试调用） |
| <http://localhost:8000/redoc> | ReDoc 文档 |

数据保存在 Compose 命名卷 `lab-data`（容器内 `/data/lab.db`）中，删除重建容器数据不丢失；`docker compose down -v` 可连同数据一起清除。

## 配置

通过环境变量配置（`docker-compose.yml` 中已设置，可按需修改）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | 容器内 `/data/lab.db`，本地运行默认 `data/lab.db` | SQLite 数据库文件路径，启动时自动建目录、建表 |

端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认 `8000:8000`）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/samples` | 登记原始样本 |
| `POST` | `/pools` | 创建混样批次 |
| `POST` | `/pool-plans` | 按目标占比反算整数份数配方（只规划，不写库） |
| `GET` | `/batches` | 列出全部批次 |
| `GET` | `/batches/{id}` | 批次详情（混样含配方） |
| `GET` | `/batches/{id}/composition` | 递归追溯原始样本占比 |
| `POST` | `/batches/{id}/recipe-preview` | 配方变更影响预演（不写库） |

## 示例请求

### 1. 登记原始样本

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"name": "S1"}'
# {"id":1,"name":"S1","kind":"sample","created_at":"...","recipe":null}

curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"name": "S2"}'
# {"id":2,"name":"S2","kind":"sample",...}
```

### 2. 创建混样批次 P1 = 1×S1 + 1×S2

```bash
curl -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"name": "P1", "recipe": [{"batch_id": 1, "portions": 1}, {"batch_id": 2, "portions": 1}]}'
```

```json
{
  "id": 3,
  "name": "P1",
  "kind": "pool",
  "created_at": "2026-09-24T12:00:00.000Z",
  "recipe": [
    {"batch_id": 1, "batch_name": "S1", "portions": 1},
    {"batch_id": 2, "batch_name": "S2", "portions": 1}
  ]
}
```

### 3. 创建二级混样 P2 = 1×P1 + 1×S1

```bash
curl -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"name": "P2", "recipe": [{"batch_id": 3, "portions": 1}, {"batch_id": 1, "portions": 1}]}'
```

### 4. 追溯 P2 的原始样本占比

```bash
curl http://localhost:8000/batches/4/composition
```

```json
{
  "batch_id": 4,
  "batch_name": "P2",
  "kind": "pool",
  "components": [
    {"sample_id": 1, "sample_name": "S1", "numerator": 3, "denominator": 4},
    {"sample_id": 2, "sample_name": "S2", "numerator": 1, "denominator": 4}
  ]
}
```

S1 经两条路径进入 P2（直接投入 1/2，加上经 P1 的 1/2×1/2），合并为 3/4；各占比之和恒为 1。

### 5. 预演 P1 配方变更的影响

提交完整替代配方（沿用创建时的校验：严格正整数份数、重复原料合并、未知批次拒绝），在内存中把 P1 的配方替换为 `3×S1 + 1×S2`，返回 P1 及其全部下游批次（此处为 P2）各原始样本的原占比、预演占比与有符号差值（`delta = preview − original`），均为约分后的分子/分母：

```bash
curl -X POST http://localhost:8000/batches/3/recipe-preview \
  -H 'Content-Type: application/json' \
  -d '{"recipe": [{"batch_id": 1, "portions": 3}, {"batch_id": 2, "portions": 1}]}'
```

```json
{
  "batch_id": 3,
  "batch_name": "P1",
  "recipe": [
    {"batch_id": 1, "batch_name": "S1", "portions": 3},
    {"batch_id": 2, "batch_name": "S2", "portions": 1}
  ],
  "affected_batches": [
    {
      "batch_id": 3,
      "batch_name": "P1",
      "kind": "pool",
      "components": [
        {"sample_id": 1, "sample_name": "S1", "original": {"numerator": 1, "denominator": 2}, "preview": {"numerator": 3, "denominator": 4}, "delta": {"numerator": 1, "denominator": 4}},
        {"sample_id": 2, "sample_name": "S2", "original": {"numerator": 1, "denominator": 2}, "preview": {"numerator": 1, "denominator": 4}, "delta": {"numerator": -1, "denominator": 4}}
      ]
    },
    {
      "batch_id": 4,
      "batch_name": "P2",
      "kind": "pool",
      "components": [
        {"sample_id": 1, "sample_name": "S1", "original": {"numerator": 3, "denominator": 4}, "preview": {"numerator": 7, "denominator": 8}, "delta": {"numerator": 1, "denominator": 8}},
        {"sample_id": 2, "sample_name": "S2", "original": {"numerator": 1, "denominator": 4}, "preview": {"numerator": 1, "denominator": 8}, "delta": {"numerator": -1, "denominator": 8}}
      ]
    }
  ]
}
```

说明：

- 预演原料只能引用**比目标批次更早创建**的批次（含目标自身在内的引用都会被 400 拒绝），预演不会成环；
- 多路径汇合的下游批次精确合并、只返回一次；未受影响的批次不出现在结果中；
- 某样本在新配方下占比降为 0 时，以 `0/1` 返回，差值为负；
- 预演**不修改数据库**：调用前后 `/batches/{id}` 与 `/batches/{id}/composition` 的结果保持原样。

### 6. 按目标占比规划混样配方

`POST /pool-plans` 接收两个**不同的已有批次 id**（`ingredient_a_batch_id`、`ingredient_b_batch_id`）、一个已有**原始样本** id（`sample_id`）、目标占比（`numerator` / `denominator`，要求 `0 ≤ 分子 ≤ 分母` 且分母 > 0）以及正整数总份数上限 `max_total_portions`。

接口沿用上面的精确占比计算：设两原料中该样本的占比分别为 `a`、`b`，新混样按正整数份数 `x:y` 混合后占比为 `(a·x + b·y)/(x + y)`。接口反算使该占比**恰好等于**目标的正整数份数，只返回**总份数最小**的整数解；当两原料占比都等于目标时任意份数都满足，按约定取 `1:1`。返回的 `recipe` 可**直接作为 `/pools` 的配方提交**，`components` 给出新混样的全部原始样本精确占比。接口只做规划，**不创建任何批次**。

以第 4 节的 P2（S1 占 3/4）和 S2（S1 占 0）为原料，规划 S1 恰好占 1/2、总份数上限 10：

```bash
curl -X POST http://localhost:8000/pool-plans \
  -H 'Content-Type: application/json' \
  -d '{"ingredient_a_batch_id": 4, "ingredient_b_batch_id": 2, "sample_id": 1, "numerator": 1, "denominator": 2, "max_total_portions": 10}'
```

```json
{
  "sample_id": 1,
  "target": {"numerator": 1, "denominator": 2},
  "total_portions": 3,
  "recipe": [
    {"batch_id": 4, "portions": 2},
    {"batch_id": 2, "portions": 1}
  ],
  "components": [
    {"sample_id": 1, "sample_name": "S1", "numerator": 1, "denominator": 2},
    {"sample_id": 2, "sample_name": "S2", "numerator": 1, "denominator": 2}
  ]
}
```

校验：`2×P2 + 1×S2` 中 S1 = 2/3×3/4 = 1/2。把 `recipe` 原样提交即可创建该混样：

```bash
curl -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"name": "P3", "recipe": [{"batch_id": 4, "portions": 2}, {"batch_id": 2, "portions": 1}]}'
```

说明：

- 目标占比**无法由正整数份数达到**时返回 **422**：包括目标落在两原料占比区间之外，或目标只等于其中一端（会迫使另一原料投入 0 份）；
- 最小整数解的**总份数超过上限**时返回 **422**，错误信息中给出最小总份数与上限；恰好等于上限时正常返回；
- 两原料 id 相同、分子 > 分母、分母 ≤ 0、份数上限非正整数等请求格式问题返回 **422**；引用不存在的批次返回 **400**；`sample_id` 指向混样批次（非原始样本）返回 **400**。

### 错误示例

```bash
# 空配方 → 422
curl -X POST http://localhost:8000/pools -H 'Content-Type: application/json' \
  -d '{"name": "P", "recipe": []}'

# 未知批次 → 400 {"detail":"未知批次 id: [999]"}
curl -X POST http://localhost:8000/pools -H 'Content-Type: application/json' \
  -d '{"name": "P", "recipe": [{"batch_id": 999, "portions": 1}]}'

# 非正份数（0 / 负数 / 小数 / 字符串）→ 422
curl -X POST http://localhost:8000/pools -H 'Content-Type: application/json' \
  -d '{"name": "P", "recipe": [{"batch_id": 1, "portions": 0}]}'

# 查询不存在的批次 → 404
curl http://localhost:8000/batches/999/composition

# 预演原料引用了比目标批次更晚创建的批次（或目标自身）→ 400
curl -X POST http://localhost:8000/batches/3/recipe-preview \
  -H 'Content-Type: application/json' \
  -d '{"recipe": [{"batch_id": 4, "portions": 1}]}'

# 规划目标无法由正整数份数达到（P2 与 S1 中 S1 占比为 3/4、1，目标 1/4 在区间之外）→ 422
curl -X POST http://localhost:8000/pool-plans -H 'Content-Type: application/json' \
  -d '{"ingredient_a_batch_id": 4, "ingredient_b_batch_id": 1, "sample_id": 1, "numerator": 1, "denominator": 4, "max_total_portions": 10}'

# 规划的最小整数解超过总份数上限（2/3 需要 2+1=3 份）→ 422
curl -X POST http://localhost:8000/pool-plans -H 'Content-Type: application/json' \
  -d '{"ingredient_a_batch_id": 1, "ingredient_b_batch_id": 2, "sample_id": 1, "numerator": 2, "denominator": 3, "max_total_portions": 2}'
```

## 占比计算说明

- 每个原始样本对自身的占比为 `1/1`；
- 混样批次中，某成分的占比 = 该成分投入份数 ÷ 本层总份数，再乘以下游展开结果，逐层递归；
- 同一原始样本经多条路径到达时，各路径占比相加合并；
- 全程使用 `fractions.Fraction` 精确有理数运算，结果自动约分，直接返回分子/分母，无浮点误差；
- 由于新批次只能引用已创建的批次，配方图无环，递归必然终止。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest                                 # 运行测试（使用临时数据库，互不影响）
```

## 项目结构

```
app/
  main.py       # FastAPI 路由与请求处理（含目标占比规划、配方变更预演端点）
  db.py         # SQLite 连接与建表（启动时自动执行）
  schemas.py    # Pydantic 请求/响应模型（含严格正整数校验）
  service.py    # 递归占比计算、目标占比反算与配方预演（Fraction 精确分数）
tests/
  test_api.py   # 端到端测试（含多级合并、约分、占比规划、配方预演、各类非法输入）
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
