# 本插件已下架（GAP-5）

自 2026-08-16 起在 `config/plugins/default_profile.yaml` 中声明
`design_generate_mcp: {enabled: false}`，默认不再加载。插件目录保留，
便于作者后续修正后恢复。

## 下架原因

manifest（`plugin.json`）的启动命令 `npx screenshot-to-code` 指向的 npm 包
是 Web 应用库，**无 bin 可执行、也不是 MCP server**：`npx` 报
"could not determine executable to run"，导致 validate-all 长期报 error，
注册表挂死一个不可用工具。

详见 `docs/working/e2e缺口修复文档_20260816.md` GAP-5。

## 恢复条件

满足其一即可恢复：删除本 DOWN.md，并把 `default_profile.yaml` 中该条目的
`enabled` 改回 `true`（或删除该条目走 defaults）。

1. npm 上出现可用的 screenshot-to-code MCP server（或等价的"截图转代码"
   MCP 实现）：替换 `plugin.json` 的 args 包名，并先本地验证
   `npx -y <包名>` 能进入 MCP stdio 协议；
2. 作者修正 manifest，指向真实存在的可执行命令。

恢复前请勿直接改回 `enabled: true`——当前命令仍会启动失败，重新把
validate-all 打红。
