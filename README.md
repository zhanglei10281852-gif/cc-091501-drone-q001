# 无人机运行管理服务基础工程

这是面向无人机空域合规、机队运行和安全处置的 Python 服务基础工程。当前已接入
**低空放行评估**:把 GeoJSON 空域、航点时序、机型能力、操作员资质与带版本通告
放进同一次放行判断,按起飞/航路/悬停/备降分段审查,输出批准、附条件批准或拒绝
及其理由;稳定规则(边界相切、跨午夜、通告撤回、并发、重启恢复)见
[docs/clearance.md](docs/clearance.md)。

## 运行

需要 Python 3.11 或更高版本(仅依赖标准库)。直接执行 `python src/index.py`
启动服务,默认监听 8000 端口;`python -m unittest discover -s tests` 执行测试,
也可以使用 `docker compose up --build` 启动容器。

## 数据持久化

落盘位置由运行时配置指定,不依赖主机隐藏状态:

- `DRONE_OPS_DB`:SQLite 数据库路径,缺省为内存库(仅进程内有效)。
  容器化部署时通过环境变量指向挂载卷即可实现重启恢复;
  所有业务状态(决定、复核任务、定时工作)都在库内,重启后自动续跑。
- `PORT` / `HOST`:监听地址。

## 使用方式

`PUT /zones/{id}` 登记空域版本,`PUT /notices/{id}` 登记通告版本,
`PUT /aircraft-models/{id}` 与 `PUT /operators/{id}` 登记机型与资质,
`POST /missions` 提交任务并同步得到放行决定,`GET /decisions/{id}/trace`
追溯完整判断过程。完整 API 与规则手册见 [docs/clearance.md](docs/clearance.md)。
