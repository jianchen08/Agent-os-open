# 排障 FAQ

> 返回 [开发指南索引](README.md)。

| 症状 | 根因与处置 |
|---|---|
| 新工具 LLM 看不到 | 三层过滤链逐层查：插件在 `config/plugins/default_profile.yaml` enabled？manifest 工具声明带齐 `input_schema`？工具名在目标 agent 的 `tool_ids` 里？ |
| 改了 plugin.json 不生效 | watcher 自动重注册（秒级）——先看内核日志有无 G2 漂移拒注册 warn（声明与实现不一致会被拒）；确认插件不是被 default_profile.yaml 显式禁用 |
| 改了插件 Python 代码不生效 | invoker 检测目录 mtime 后 kill + respawn；确认没在 stdout print（破坏 JSON-RPC，日志走 stderr） |
| sidecar 起不来，报 `PYPROJECT_MISSING` / `VENV_INTERPRETER_MISSING` | 插件目录缺 `pyproject.toml` 或 `.venv`——`uv sync --project <插件目录>` 重建；内核不回退 PATH 裸 python |
| native 插件报产物缺失 | `host_type: in_process` 且 `native.artifact` 声明的 cdylib 不在插件目录——`cargo build --release` 后把产物复制到插件目录根，文件名与 `entry` 一致 |
| 流式事件被网关拒绝 | manifest 未声明 `capabilities.streaming`（fail-closed），按 `docs/guides/streaming-protocol.md` 补声明（watcher 自动重注册生效） |
| 工具结果前端渲染不对 | `output_schema` / `render` 声明缺失或与返回不符——契约 fail-closed，按实际返回结构补齐 |
| service 方法别的插件调不到 | `services` 不进 LLM 面；调用方声明 `requires_services`（角色名），boot 期闸不满足内核拒启 |
| 管道改完不生效 | 管道配置是热重载（每次执行前 mtime 检测，1s TTL），先确认文件已保存且配置能通过校验：坏 YAML / 命名冲突 / 编译错误会静默保留旧配置继续跑，内核日志有 warn |
| agent 换了工具白名单不生效 | agent yaml 热生效但只对新任务；确认工具本身已启用（第一条） |
| 前端插件表单/页面没更新 | ui_schema 变化需刷新前端页面 |
