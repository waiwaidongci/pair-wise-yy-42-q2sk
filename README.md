# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/dispatch.py`：派工门禁业务（到场/离场、迟到补录、疲劳派工留档、待派重判）。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页（含派工门禁操作区）。
- `tests/`：完整流程、规则、派工门禁和失败测试。

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

派工门禁（疲劳判定在`src/rules.py`，派工留档在`src/dispatch.py`）：

- `POST /api/attendance/check-in`：到场登记（`member_code`、可选`arrival_at`/`item_id`）。同一队员未离场前重复到场直接沿用首次结果。
- `POST /api/attendance/check-out`：离场登记（`member_code`、可选`depart_at`），随后自动重判该队员的待派记录。
- `POST /api/attendance/backfill`：迟到补录到场+离场；与已登记到场时刻一致时补全/更正离场时刻，并重新判定待派记录。
- `POST /api/dispatches`：新派工，按最近一次离场计算；连续作业超过8小时或休息不足2小时只进`held`（待休整），返回超限时长、可派工时、最早可派时刻和原因，达标则为`assigned`。
- `GET /api/dispatches`、`GET /api/attendance`（支持`member_code`/`status`过滤）、`GET /api/members/{code}/status`。现场记录始终可查。

允许角色：field_commander, incident_commander, logistics, viewer。到场/离场允许field_commander、logistics；派工仅logistics。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中，未离场队员不能被重复派入。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
