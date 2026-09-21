# 潮位通航窗口 API（FastAPI + SciPy + SQLite）

根据按 UTC 递增的潮位样本，使用 **PCHIP（单调保形三次 Hermite）插值** 求出潮高
不低于安全水深（`吃水 + 最小富余`）的全部连续通航窗口；阈值交点用 Brent 法精确
求根并**保守取整到秒**。相邻样本间隔超过 `max_gap_seconds` 的区段**禁止插值**，
作为 `unknown` 返回，通航窗口不会跨越该区段。

创建任务时还可传入 `transit_duration_seconds`（全程航程占用时长）与
`maintenance_blocks`（维护封航区间，UTC 起止）。传入航程时长后，结果新增
`departure_windows`：仅返回**出发时刻 `d` 的占用区间 `[d, d+航程时长)` 全程落在
同一个安全通航窗口内**（因而绝不跨越 `unknown`）、**且不与任何封航区间相交**的出发
时段。封航区间会先裁剪到查询范围并合并重叠/首尾相接区间，再整体向过去平移一个航程
时长后，从候选出发区间中扣除。

计算以**异步任务**方式执行：先创建任务拿到 ID，再按 ID 轮询结果；任务状态与
结果持久化在 SQLite 中（容器启动时自动建库建表）。

## 一条命令启动

前置条件：已安装 Docker 与 Docker Compose 插件（v2）。

```bash
docker compose up --build -d
```

启动后访问：

- Swagger 交互文档：<http://localhost:8000/docs>
- ReDoc 文档：<http://localhost:8000/redoc>
- OpenAPI 描述：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

停止 / 查看日志：

```bash
docker compose down        # 停止（数据保留在命名卷 tide-data 中）
docker compose logs -f
```

## 必要配置

| 配置 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TIDE_DB_PATH` | 环境变量（compose 中） | `/data/tide.db` | SQLite 数据库文件路径，启动时自动创建目录、文件与表结构 |
| 端口映射 | `docker-compose.yml` | `8000:8000` | 左侧为宿主机端口，如被占用可改为 `8080:8000` |
| 数据卷 | `docker-compose.yml` | `tide-data:/data` | 数据库持久化卷，`docker compose down` 后数据仍保留 |

本地（非容器）运行：

```bash
pip install -r requirements.txt
TIDE_DB_PATH=./data/tide.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### 1. 创建计算任务

`POST /tasks` → `201 Created`，立即返回任务 ID（`status` 初始为 `pending`），
计算在后台进行。

请求体：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `samples` | 数组（≥2） | 潮位样本，按 UTC 时间**严格递增**：`{"time", "height_m"}` |
| `query_start` / `query_end` | datetime | 查询起止时间（ISO-8601，建议带 `Z`；无时区按 UTC 处理） |
| `draft_m` | number ≥ 0 | 船舶吃水（米） |
| `clearance_m` | number ≥ 0 | 船底最小富余水深（米） |
| `max_gap_seconds` | number > 0 | 相邻样本最大允许间隔（秒），超过则该区段禁止插值 |
| `transit_duration_seconds` | number ≥ 0，可选 | 全程航程占用时长（秒）。传入后结果含 `departure_windows`；不传则保持原行为（该字段返回空数组） |
| `maintenance_blocks` | 数组，可选 | 维护封航区间 `[{"start", "end"}]`（UTC 起止）。自动裁剪到查询范围，并合并重叠或**首尾相接**区间；仅在传入航程时长时生效 |

> 潮高、吃水、富余必须使用**同一垂直基准面**，单位统一为米。
> 安全水深阈值 = `draft_m + clearance_m`。
>
> 航程占用按**半开区间** `[departure, departure + transit_duration_seconds)` 处理；
> 封航区间同为半开 `[start, end)`，占用区间与封航区间仅在端点相切时不算相交。
> 出发时刻与窗口边界均保守取整到整秒。

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "samples": [
      {"time": "2026-09-21T00:00:00Z", "height_m": 1.0},
      {"time": "2026-09-21T01:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T02:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T03:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T04:00:00Z", "height_m": 8.0},
      {"time": "2026-09-21T05:00:00Z", "height_m": 6.0},
      {"time": "2026-09-21T06:00:00Z", "height_m": 3.0},
      {"time": "2026-09-21T07:00:00Z", "height_m": 1.0}
    ],
    "query_start": "2026-09-21T00:00:00Z",
    "query_end":   "2026-09-21T07:00:00Z",
    "draft_m": 4.0,
    "clearance_m": 1.0,
    "max_gap_seconds": 3700,
    "transit_duration_seconds": 3600,
    "maintenance_blocks": [
      {"start": "2026-09-21T04:30:00Z", "end": "2026-09-21T05:00:00Z"},
      {"start": "2026-09-21T05:00:00Z", "end": "2026-09-21T05:30:00Z"},
      {"start": "2026-09-21T06:30:00Z", "end": "2026-09-21T09:00:00Z"}
    ]
  }'
```

上例中前两段封航首尾相接，合并为 `04:30–05:30`；第三段超出查询范围，裁剪为
`06:30–07:00`（平移后不影响本窗口内的候选出发）。

### 2. 按 ID 查询任务

`GET /tasks/{task_id}` → `200`（不存在返回 `404`）。

`status`：`pending`（计算中）/ `done`（结果见 `result`）/ `failed`（原因见 `error`）。

`result.windows[]`（**全部**连续安全窗口，起止时间为整秒、UTC `Z` 结尾）：

| 字段 | 说明 |
| --- | --- |
| `start` / `end` | 窗口起止（保守取整：交点向上/向下取整秒，保证窗口内任意时刻满足水深） |
| `start_height_m` / `end_height_m` | 窗口边界处插值潮高（米） |
| `min_height_m` | 窗口内插值潮高最小值（米，由内部样本点与边界取小者） |
| `duration_seconds` | 窗口时长（秒） |

`result.unknown[]`（无法判定的区段，窗口不会跨越）：

| `reason` | 含义 |
| --- | --- |
| `sample_gap_exceeds_max_gap` | 相邻样本间隔超过 `max_gap_seconds`，按要求禁止插值 |
| `no_sample_coverage` | 查询范围超出全部样本覆盖区间（不做外推） |

`result.departure_windows[]`（**仅当传入 `transit_duration_seconds` 时非空**；
否则为 `[]`，原 `windows` / `unknown` 行为不变）。每个时段为半开区间
`[start, end)`，整秒，UTC `Z` 结尾：

| 字段 | 说明 |
| --- | --- |
| `start` | 最早可行出发时刻 |
| `end` | 出发时段右端点（**半开**：该时刻本身不可出发，最后一个可行出发为 `end - 1s`） |
| `duration_seconds` | 可行出发秒数（`end - start`） |

示例响应（对应上文请求，节选）：

```json
{
  "task_id": "002458c031004733a6172a2d9add2027",
  "status": "done",
  "created_at": "2026-09-21T05:16:13.811445Z",
  "result": {
    "threshold_m": 5.0,
    "max_gap_seconds": 3700.0,
    "windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T05:20:49Z",
        "start_height_m": 5.001,
        "end_height_m": 5.001,
        "min_height_m": 5.001,
        "duration_seconds": 13298
      }
    ],
    "unknown": [],
    "departure_windows": [
      {
        "start": "2026-09-21T01:39:11Z",
        "end": "2026-09-21T03:30:00Z",
        "duration_seconds": 6649
      }
    ]
  },
  "error": null
}
```

示例解读：安全窗口为 `01:39:11–05:20:49`（闭区间，端点整秒安全）。航程 1 小时，
故候选出发时段为 `01:39:11–04:20:49`（即 `04:20:49` 出发时占用区间
`[04:20:49, 05:20:49)` 恰好结束在安全窗口右端点）。封航段 `04:30–05:30` 向过去
平移 1 小时得到禁出发段 `03:30–04:30`，从候选中扣除后只剩
`01:39:11–03:30:00`（半开）：`03:30:00` 出发时占用区间为 `[03:30,04:30)`，与
封航段在 `04:30` 端点相切，按半开区间约定不算相交，故 `03:30:00` 为最后一个可行
出发；`03:30:01` 起占用区间即与封航段相交。另一段 `06:30–07:00` 平移为
`05:30–06:00`，完全落在候选之外，不影响结果。

校验失败（如样本未严格递增、吃水为负等）统一返回 `422` 并指明字段与原因。

## 算法说明

1. **时间数值化**：所有时间转 POSIX 秒；无时区输入按 UTC 处理。
2. **链分割**：相邻样本间隔 `> max_gap_seconds` 处切分为若干独立样本链。
   每条链单独构造 PCHIP，**任何三次分段都不会跨越该间隙**；孤立样本点
   （链长为 1）不产生有效覆盖。
3. **PCHIP 插值 + 内部极值**：PCHIP 保形（不会超过数据点极值），但单个三次
   分段在非单调数据附近仍可能出现内部极值。因此对每条链用
   `derivative().roots()` 取全部驻点，与样本节点共同构成事件网格——相邻事件
   之间曲线单调，是否跨阈值只需比较两端符号。
4. **阈值求根**：对跨阈值的相邻事件用 `scipy.optimize.brentq`（容差 `1e-6` 秒，
   远高于秒级要求）精确求出交点。
5. **保守取整到秒**：窗口起点 `ceil`、终点 `floor`，舍去不足整秒的窗口，
   保证报告的每一秒潮高都不低于安全水深。
6. **unknown 互补集**：查询区间内未被任何有效链覆盖的部分即为 `unknown`，
   并区分“超间隔（禁止插值）”与“无样本覆盖（不外推）”两种原因；
   窗口严格落在覆盖区内，绝不跨越未知段。
7. **航程占用与出发时段**（传入 `transit_duration_seconds` 时）：对每个安全窗口
   `[ws, we]`，候选出发时刻满足 `[d, d+D) ⊆ [ws, we]`（`D` 为航程时长），
   保守取整到整秒；由于安全窗口本身不跨越 `unknown`，候选出发也绝不跨越未知段。
   维护封航区间先裁剪到查询范围、合并重叠或首尾相接区间，再将每段 `[a, b)`
   整体向过去平移 `D` 得到禁出发段 `[a-D, b-D)`，从候选中逐段扣除；
   占用与封航均为半开区间，仅端点相切不视为相交。

## 项目结构与测试

```
app/
  main.py            # FastAPI 路由、任务创建/查询、后台计算
  schemas.py         # 请求/响应模型与输入校验
  config.py          # 配置（TIDE_DB_PATH 等）
  db.py              # SQLite 连接与启动建表
  services/tides.py  # PCHIP 插值、求根、窗口与 unknown 计算、航程占用/封航扣除
  tests/             # pytest：算法单测 + 接口端到端测试
Dockerfile           # 依赖安装于构建阶段
docker-compose.yml   # 一条命令启动，含卷持久化与健康检查
requirements.txt
```

运行测试（本地）：

```bash
python -m pytest app/tests -q
```
