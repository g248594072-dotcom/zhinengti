# 1Panel / Docker 的 MySQL 连接修复

网页容器中的 `127.0.0.1` 指向网页容器自身；即使 MySQL 和网页在同一台服务器，也不能据此填写 `127.0.0.1`。先在 1Panel「容器」确认网页 `chat-qc` 和 MySQL 的部署位置及网络。

当前部署脚本的配置位置为 `/opt/tool-platform/agent-qc/config/.env`，容器内为 `/app/config/.env`。以网页「数据库连接检查与配置」显示的实际路径为准；进程中的 `MYSQL_*` 环境变量优先于此文件。

- MySQL 也在容器中：让网页与 MySQL 使用同一个已有 Docker 网络，`MYSQL_HOST` 填该网络中的 MySQL 服务名或网络别名，`MYSQL_PORT` 填 MySQL 容器的监听端口（通常为 3306）。服务名从 1Panel 中核对，不要凭空填写 `mysql`。
- MySQL 在宿主机：`MYSQL_HOST` 填网页容器能够访问的宿主机地址，`MYSQL_PORT` 填对应端口。Linux 可使用 `host.docker.internal`，但必须在网页服务的 Compose 中配置 `extra_hosts: ["host.docker.internal:host-gateway"]`，且 MySQL 已允许来自该容器网络的连接。
- 直接在宿主机启动网页、MySQL 也在宿主机时，才可使用 `127.0.0.1`。

保留现有的 `MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`，只改核实后的连接目标。不要把含密码的 `.env` 发到聊天或提交 Git。更新脚本现在保留已有 `config/`，不会用代码目录中的旧配置覆盖它。重建前还会备份旧网页容器中两份已审核知识库规则并放入新镜像，备份保留在 `config/knowledge-backups/`。

修改挂载的 `.env` 后，在网页展开「数据库连接检查与配置」，点击「测试数据库连接 / 重新连接」，成功后重试知识库导出。如果 Compose 中显式配置了 `MYSQL_HOST` 等变量，需要在 1Panel 中修改对应环境变量并重建网页容器，因为环境变量优先。

也可以在 1Panel「终端」执行以下只读检查（假设网页容器名为 `chat-qc`）：

```sh
docker exec chat-qc python -c 'from db import test_mysql_connection; test_mysql_connection(); print("MySQL connection OK")'
```

检查只执行 `SELECT 1`，不建表或修改业务数据。连接拒绝表示目标主机/端口不可达；1045 表示登录或授权不正确；1049 表示库名不存在。

## 删除客户拉取的新规则

网页「上传删除客户」可选择沟通起止日期、时间，包含开始、不含结束。先匹配删除标签、排除黑粉，再拉完整聊天；仅保留范围内有客户有效沟通、且完整历史客户有效发言至少 7 条的客户。聊天记录中的图片和单独的「1」沿用现有计数规则，不计为有效发言。符合者保存完整聊天，不调用 AI；导入后可另行触发流失复盘。

每日脚本仍默认筛选昨天的沟通，也支持任意范围，例如：

```sh
python fetch_delete_daily.py --start 2026-10-01T00:00:00 --end 2026-10-22T00:00:00 --dry-run
```

移除 `--dry-run` 才会入库。新构建的网页容器时区固定为 Asia/Shanghai。

接口流程依据项目使用的 [SaleSmartly 访客标签列表接口](https://salesmartly-api.apifox.cn/获取访客标签列表-456389145e0) 和 [客户列表接口](https://salesmartly-api.apifox.cn/获取客户列表-258167563e0)，先获取删除标签 ID，再按标签 ID 获取客户；沟通范围以聊天记录实际时间为准。
