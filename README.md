# Recovery Planner（恢复规划 HTTP API）

基于 FastAPI + SQLite 的恢复规划服务：批量登记数据库快照与日志片，针对给定 UTC 查询时刻，
计算最优恢复方案（快照 + 同格式日志片连续回放），返回快照、逐片实际回放区间与恢复终点序号；
并支持按受保护时刻批量清理已登记记录——逐时刻校验删除前后最佳终点不下降后才原子删除。

## 快速启动（Docker，单命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口（宿主机 `8000` → 容器 `8000`）。镜像内已安装全部依赖，
并在构建时与服务启动时（幂等）初始化 SQLite schema；数据库文件位于容器内 `/data/recovery.db`，
通过命名卷 `recovery-data` 持久化。交互式 API 文档见 `http://localhost:8000/docs`。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000     # DB_PATH 环境变量可指定数据库路径，默认 ./recovery.db
pytest                               # 运行测试
```

## 数据模型与规则

**快照 snapshot**：`id`（唯一，非空字符串）、`created_at`（UTC 生成时间，ISO 8601）、
`applied_seq`（全局非负整数序号）、`format`（格式代号，非空字符串）。

**日志片 log segment**：`id`（唯一）、`created_at`（UTC 生成时间）、`format`（格式代号）、
`first_seq..last_seq`（正整数闭区间，`1 <= first_seq <= last_seq`）。

**批量登记 `POST /batches`**：

- 请求体 `{"snapshots": [...], "log_segments": [...]}`，两表合计**至多 100 条**；
- `id` 在快照与日志片之间全局唯一（批内及与库中已有记录均不得重复）；
- 重复 id、字段非法（类型/取值/多余字段）或超过 100 条时，**整批拒绝、一条不入库**
  （字段/批内校验失败返回 `422`，与库中记录冲突返回 `409`）；
- 时间可带任意时区偏移（如 `+02:00`）或为朴素时间（视为 UTC），一律归一化为 UTC 存储。

**恢复规划 `GET /recovery-plan?at=<UTC 时刻>`**：

- 只使用 `created_at <= at` 的快照与日志片（含等于）；
- 从快照 `applied_seq + 1` 起，仅用**同格式**日志片连续回放：每片从下一未回放序号接至片尾，
  重叠部分不重放，遇缺口即停；
- 优化目标：最大化恢复终点序号；同分依次取 `applied_seq` 最大、`id` 字典序最小的快照，
  再取片数最少、回放 id 序列字典序最小的链；
- 无可用快照时返回 `404`，明确无恢复点。

**受保护时刻批量清理 `POST /cleanup`**：

- 请求体 `{"ids": [...], "moments": [...]}`：`ids` 为 **1～100 个互异**的已登记记录 id
  （快照、日志片可混填），`moments` 为 **1～8 个 UTC 查询时刻**（允许重复，按请求顺序逐一评估）；
- 请求内 id 重复、数量越界、字段非法时返回 `422`，整批拒绝；存在库中没有的 id 时返回 `409`，
  同样一条也不删；
- 逐个保护时刻，按上面的规划规则比较**删除前**与**整批删除后**的最佳恢复终点：仅当**所有时刻**
  的终点都不下降时才真正删除。不要求保留原选中的快照或原回放链，终点由其他记录保持即可；
- 无恢复点以 `null` 表示，比较时低于任何序号；因此 `null -> null`（该时刻始终无快照）与
  `null -> 序号`（该时刻本来就无恢复点）都不算下降，而 `序号 -> null` 算下降；
- 任一时刻下降则**一条也不删**，返回 `409`，`detail.decreased` 列出**所有**下降时刻及其
  删除前后终点；成功返回 `200`、已删除 id 列表以及各时刻保留的终点；
- 比较与删除在**同一个数据库事务**（`BEGIN IMMEDIATE`）中完成，基于同一份数据库状态，
  失败（含并发冲突）不会留下部分删除。

## HTTP 示例（可直接执行）

### 1. 登记一批快照与日志片

```bash
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "snapshots": [
      {"id": "snap-1", "created_at": "2026-09-01T08:00:00Z", "applied_seq": 100, "format": "wal-v1"},
      {"id": "snap-2", "created_at": "2026-09-01T12:00:00Z", "applied_seq": 160, "format": "wal-v1"}
    ],
    "log_segments": [
      {"id": "seg-1", "created_at": "2026-09-01T09:00:00Z", "format": "wal-v1", "first_seq": 101, "last_seq": 150},
      {"id": "seg-2", "created_at": "2026-09-01T10:00:00Z", "format": "wal-v1", "first_seq": 140, "last_seq": 180},
      {"id": "seg-3", "created_at": "2026-09-01T11:00:00Z", "format": "wal-v1", "first_seq": 182, "last_seq": 200},
      {"id": "seg-9", "created_at": "2026-09-01T10:30:00Z", "format": "wal-v2", "first_seq": 101, "last_seq": 999}
    ]
  }'
# => 201  {"stored_snapshots": 2, "stored_log_segments": 4}
```

### 2. 查询恢复计划

```bash
curl -s 'http://localhost:8000/recovery-plan?at=2026-09-02T00:00:00Z'
```

响应（`snap-1` 起回放：`seg-1` 覆盖 101..150，`seg-2` 与其重叠、只回放 151..180；
`seg-3` 在 181 处出现缺口而停止；`seg-9` 格式不符被忽略；终点 180 优于 `snap-2` 的 160）：

```json
{
  "snapshot": {"id": "snap-1", "created_at": "2026-09-01T08:00:00+00:00", "applied_seq": 100, "format": "wal-v1"},
  "replay": [
    {"id": "seg-1", "from_seq": 101, "to_seq": 150},
    {"id": "seg-2", "from_seq": 151, "to_seq": 180}
  ],
  "endpoint": 180
}
```

### 3. 无恢复点（查询时刻之前没有任何快照）

```bash
curl -s 'http://localhost:8000/recovery-plan?at=2020-01-01T00:00:00Z'
# => 404  {"detail": "no recovery point: no snapshot created at or before 2020-01-01T00:00:00+00:00"}
```

### 4. 受保护时刻批量清理（成功）

删除对任何保护时刻都没有影响的记录。下例中 `seg-9` 是 `wal-v2` 格式，而快照均为 `wal-v1`，
从未被任何回放链使用，因此两个保护时刻的终点（均为 180）保持不变：

```bash
curl -s -X POST http://localhost:8000/cleanup \
  -H 'Content-Type: application/json' \
  -d '{
    "ids": ["seg-9"],
    "moments": ["2026-09-01T11:00:00Z", "2026-09-02T00:00:00Z"]
  }'
```

响应（`200`，返回已删除 id 和各时刻保留的终点，顺序与请求一致）：

```json
{
  "deleted_ids": ["seg-9"],
  "endpoints": [
    {"at": "2026-09-01T11:00:00+00:00", "endpoint": 180},
    {"at": "2026-09-02T00:00:00+00:00", "endpoint": 180}
  ]
}
```

### 5. 批量清理被保护时刻阻止（一条也不删）

删除 `snap-2` 与 `seg-2` 后，2026-09-02 的最佳终点会从 180 下降到 150（退化为 `snap-1` 起
回放 `seg-1`），整批被拒：

```bash
curl -s -X POST http://localhost:8000/cleanup \
  -H 'Content-Type: application/json' \
  -d '{
    "ids": ["snap-2", "seg-2"],
    "moments": ["2026-09-02T00:00:00Z"]
  }'
# => 409
```

```json
{
  "detail": {
    "message": "best recovery endpoint decreases at one or more protected moments; nothing was deleted",
    "decreased": [
      {"at": "2026-09-02T00:00:00+00:00", "before": 180, "after": 150}
    ]
  }
}
```

无恢复点用 `null` 表示、低于任何序号：若删除导致某时刻由有终点变为无恢复点，则该时刻报
`"before": <序号>, "after": null`；反之某时刻删除前后都没有快照（`null -> null`）则允许删除。

整批拒绝的其他情形：

```bash
# 请求内 id 重复 / 数量越界 / 字段非法：422
curl -s -X POST http://localhost:8000/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"ids": ["seg-1", "seg-1"], "moments": ["2026-09-02T00:00:00Z"]}'

# id 不存在（即使同批其他 id 存在）：409，已存在的也不会被删
curl -s -X POST http://localhost:8000/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"ids": ["seg-1", "ghost"], "moments": ["2026-09-02T00:00:00Z"]}'
# => 409  {"detail": "id(s) not registered: ['ghost']; request rejected"}
```

### 6. 整批登记拒绝示例

```bash
# 重复 id（与库中已有记录冲突）：整批不入库
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{"snapshots": [{"id": "snap-1", "created_at": "2026-09-03T00:00:00Z", "applied_seq": 5, "format": "wal-v1"}]}'
# => 409  {"detail": "id(s) already registered: ['snap-1']; batch rejected"}

# 非法字段（first_seq > last_seq）：422，整批不入库
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{"log_segments": [{"id": "bad", "created_at": "2026-09-03T00:00:00Z", "format": "wal-v1", "first_seq": 10, "last_seq": 3}]}'
# => 422  {"detail": [... "last_seq (3) must be >= first_seq (10)" ...]}
```

### 7. 健康检查

```bash
curl -s http://localhost:8000/health    # => {"status": "ok"}
```

## 项目结构

```
app/
  main.py      # FastAPI 应用与端点（启动时初始化 SQLite）
  models.py    # 请求校验（pydantic，严格类型）
  planner.py   # 回放链规划：DAG 动态规划，实现全部平局裁决规则
  db.py        # SQLite 连接、建表、批量原子写入
tests/test_api.py
Dockerfile / docker-compose.yml
```
