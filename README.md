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
- `GET /api/audit`

### 任务区与资源占用

- `POST /api/zones`、`GET /api/zones`、`GET /api/zones/{id}`
- `POST /api/zones/{id}/close`，必须提交`expected_version`
- `POST /api/resources`、`GET /api/resources`
- `POST /api/assignments`：派单（resource_id、zone_id、start_at，可选end_at）
- `POST /api/assignments/release`：撤离（resource_id、zone_id、end_at）
- `GET /api/zones/{id}/assignments`、`GET /api/resources/{id}/assignments`

资源占用按半开时段`[start_at, end_at)`记录，`end_at`为空表示仍在场。派单时若该资源在
重叠时段（端点相接的班次交接不算重叠）已被占用，返回`409`并在`details.conflicts`中
指出冲突任务区，本次分配不落库。

### 离线队伍批次回传

- `POST /api/offline-reports`：同一`batch_no`回传`departed`/`arrived`/`evacuated`，
  每条含`resource_id`、`zone_id`、`field_time`；同一批次必须属于同一资源与同一任务区。
- `GET /api/offline-reports?status=pending&zone_id=...`
- `POST /api/offline-reports/{id}/review`：调度员`confirm`或`reject`矛盾记录。

重复上传按最早的现场时间合并（同一`batch_no+event`只保留一行并累加`upload_count`）；
出发→到场→撤离时序矛盾（到场早于出发、撤离早于到场、没有到场就撤离）时，后到的矛盾
状态标记为`pending`并写入`conflict_note`，不产生占用，等调度员确认。确认后才同步占用
区间；已驳回记录不会被后续重复上传悄悄改回。

### 任务区关闭不变量

任务区关闭前，未撤离资源（开放占用）和未确认（pending）离线记录都会作为
`close_blockers`，二者为零时才允许关闭。

允许角色：field_commander, incident_commander, logistics, viewer。任务区/资源建立为
field_commander/incident_commander，派单为incident_commander/logistics，离线回传为
field_commander/logistics，矛盾记录确认为incident_commander。火线长度、风向变化和
离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
