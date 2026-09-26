# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/fatigue.py`：疲劳判定，按最近一次离场计算连续作业与休息时长。
- `src/dispatch.py`：到场/离场登记、派工门禁留档、迟到补录后待派记录重新判定。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `static/dispatch.html`：派工门禁页面（登记、派工判定、疲劳预判、留档查询）。
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
- `POST /api/attendance`：登记到场/离场时刻，同一队员重复到场沿用首次结果
- `GET /api/attendance?member=`：现场记录查询
- `POST /api/dispatches`：新派工门禁判定，超限只进待休整并给出超限时长与可派工时
- `GET /api/dispatches?member=&status=`：派工留档查询
- `GET /api/fatigue?member=`：疲劳预判
- `GET /dispatch`：派工门禁页面

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。派工门禁按最近一次离场计算：连续作业超过8小时或休息不足2小时只进待休整；迟到补录改变资格时待派记录重新判定，现场记录保留可查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
