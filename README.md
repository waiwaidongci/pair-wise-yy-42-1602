# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/zones`、`GET /api/zones?item_id=`：任务区（active/closed）
- `POST /api/resources`、`GET /api/resources`：救援队（team）和车辆（vehicle）
- `POST /api/occupancies`：派单，按`arrived_at`/`withdrawn_at`检测同一资源的时段重叠，冲突返回409并在`details.conflicts`中指出冲突任务区
- `POST /api/occupancies/{id}/withdraw`、`GET /api/occupancies?zone_id=&resource_id=`
- `POST /api/offline-batches`：离线批次回传，`reports`为`{status,event_time}`数组，status取`departed`/`arrived`/`withdrawn`
- `POST /api/offline-batches/{id}/confirm`：确认`{index,accepted}`或驳回挂起的矛盾状态
- `GET /api/offline-batches`、`POST /api/zones/{id}/transition`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

### 任务区与资源占用规则

- 资源随班次到场/撤离：派单给出`arrived_at`和可选`withdrawn_at`（半开区间，首尾相接不算重叠）；未给撤离时间视为持续在场。
- 派单与该资源任意任务区的已有占用重叠时，分配被拒绝（409），响应列出冲突的任务区编号、名称和时段。
- 离线队伍用同一`batch_no`重复回传时按最早现场时间合并：同阶段更晚的时间记为重复；更早或违反出发→到场→撤离顺序的矛盾状态进入`pending`，不改动占用，等待调度员确认。
- 确认接受的矛盾记录会同步到资源占用（到场/撤离时间）；驳回则保留原值。
- 任务区关闭前，未撤离资源数（`active_occupancies`）和未确认离线记录数（`pending_offline`）都必须为零，否则409；关闭后不再接受新派单。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
