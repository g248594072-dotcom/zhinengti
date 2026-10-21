# 每日自动导入和分析

统一入口 `python nightly_pipeline.py`，在 chat-qc 容器内使用网页相同的持久配置。
服务器执行 `bash /opt/zhinengti/deploy/install-nightly.sh`，安装每天北京时间 02:00 的 systemd timer；不依赖宿主机时区或本地电脑在线。安装前先做只读配置和数据库检查，备份并替换原来该项目的四条 cron，其他任务保留。

任务处理前一天的成交日期标签与删除客户沟通窗口。删除客户先筛删除标签、排除黑粉，再拉完整历史，要求窗口内有客户有效发言且完整历史至少7条。成交和删除分别入库，任一方失败不阻止另一方。导入检查部分失败和冲突，不推进失败日期；下次从上次成功日期补拉。首次运行从昨天开始。重复导入沿用会话ID和消息增量去重。

入库后自动分析全部新增、空规则或聊天有变化的客户。相似案例只作参考，每位客户均实际调用AI，不再复制参考客户的画像或分析。旧的“继承跳过”记录每类每晚补跑50位（`--repair-limit`），优先处理新增客户。单次使用待分析快照，失败留到下一次，避免当晚无限重试；分析失败不覆盖原结果。审核后的知识库仍保留人工审核流程，自动分析只写数据库。

持久状态：`/opt/tool-platform/agent-qc/config/nightly_pipeline_state.json`；OS文件锁防止统一任务并发。定时任务日志：`journalctl -u chat-qc-nightly.service`；检查：`systemctl list-timers chat-qc-nightly.timer`。只检查、不导入、不调用AI：`docker exec chat-qc python /app/nightly_pipeline.py --check`。手动执行完整任务：`systemctl start chat-qc-nightly.service`。

旧cron备份位于config/schedule-backups/。停止自动执行：`systemctl disable --now chat-qc-nightly.timer`。代码部署仍使用update-chat-qc.sh，不会覆盖持久配置和知识库。
