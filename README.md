# 无人机运行管理服务基础工程

面向城市低空运行中心的放行判断服务：把 GeoJSON 空域、航点时序、机型能力、
操作员资质和带版本的通告放到同一次放行判断中，按起飞、航路、悬停、备降等
分段审查，输出批准、附条件批准或拒绝及其理由。

## 运行

需要 Python 3.11 或更高版本（仅依赖标准库）。直接执行 `python src/index.py`
启动服务，默认监听 8000 端口；`python -m unittest discover -s tests` 执行
全部测试，也可以使用 `docker compose up --build` 启动容器。

运行时配置（环境变量）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | 监听地址 |
| `RELEASE_DATA_DIR` | `./data` | SQLite 数据目录，重启后状态在此恢复 |
| `RELEASE_TZ` | `Asia/Shanghai` | 每日时段窗使用的本地时区 |
| `RELEASE_SCHED_INTERVAL` | `1.0` | 定时生效事件处理间隔（秒） |
| `RELEASE_SCHEDULER` | `1` | 置 `0` 关闭后台调度线程 |

## API 概览

- `POST /airspaces` 登记空域新版本（GeoJSON Polygon/MultiPolygon，
  `kind=no_fly|restricted`，限飞区须带 `max_altitude_m`）
- `POST /notices` 发布通告新版本（`op=publish`）或撤回（`op=withdraw`），
  版本号由服务端按 id 递增
- `POST /missions` 提交任务与计划（含机型、操作员、分段航点），同步完成放行评估
- `POST /missions/{id}/plans` 提交计划新版本（`expected_version` 乐观并发）
- `POST /missions/{id}/evaluate` 基于当前计划重新评估，可附人工意见与改判
- `GET /decisions/{id}` 决策全文：分段审查轨迹 + 固化快照（空域版本与校验和、
  通告版本、规则版本、人工意见）
- `GET /reviews?status=pending` 待复核清单；`POST /reviews/{id}/resolve` 处理复核
- `GET /reminders?status=open` 提醒列表

## 关键语义

- **分段审查**：起飞、航路、悬停、备降、返航、降落各段独立判定，
  任务结论取各段最严结果；任何一段拒绝则任务拒绝，否则有条件则附条件批准。
- **边界相切**：空域按闭集处理，航线与边界相切、触边、压线均视为进入。
- **跨午夜**：全部比较基于带时区的绝对时刻，无需特判；每日时段窗
  （如 22:00–06:00）结束不晚于开始时跨零点回绕。
- **通告撤回**：撤回是通告的新版本；评估只采用决策时刻的最新版本，
  已固化的历史决策不受撤回影响。
- **新禁飞区**：通告发布或定时生效时扫描已放行任务，只生成待复核条目与
  提醒，绝不改写原决策；复核可触发重新评估，新决策追加为下一序号。
- **并发提交**：`(任务, 版本)` 唯一约束加事务保证相同任务的并发提交
  只有一个有效版本，失败方收到 409。
- **重启恢复**：决策、复核、提醒、定时生效事件全部落盘于
  `RELEASE_DATA_DIR`，进程重启后继续未完成复核与定时生效工作。

详细领域规则见 `docs/domain.md`。
