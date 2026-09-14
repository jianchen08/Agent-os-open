# research 模式包 · 检索/摘要工具面清单（含健康标注）

> 核验时间：2026-09-13（批次 R5v2）
> 标注口径：✅ 在册（声明/档案证据齐备）｜⚠️ 已知问题（如实标注，不掩盖）｜⛔ 已退役（禁止声明）
> 本清单为「按实况声明」的依据：3 角色的 `tool_ids` 只允许出现本清单「一、在册工具」中的名称。

---

## 一、在册工具（本包声明）

| 工具 | 所属插件 | 用途 | 健康状态 | 使用角色 | 来源 |
|------|----------|------|----------|----------|------|
| `universal_search` | omnisearch（external MCP） | 聚合检索：28+ 源（学术/StackOverflow/GitHub/HackerNews/Wikipedia/HuggingFace/SearXNG），index 广播 + full 取全文 | ✅ 在册。ADR 记录「运行正常、8 工具注册成功、已在四份 agent 白名单中」 | 检索 | [来源: docs/decisions/2026-09-13-orphan-mcp-search-plugins-removed.md；plugins/shared/tools/external_mcp/omnisearch/plugin.json] |
| `web_operate` | web_ext（web_operate_tool） | HTTP 请求与网页抓取（get/post/fetch，含正文抽取） | ✅ 在册（声明 + 测试齐备：test_web_tool.py 等）；运行态未在本任务内实测 | 检索 | [来源: plugins/shared/tools/web_ext/plugin.json] |
| `enhanced_search` | agentos-builtin-tools | 代码/文件搜索（内容 + 文件名） | ⚠️ **已知卡顿**：全仓扫描分钟级——2026-09-13 实测单次 15m13s（触发任务 D1 修复中）。本任务实测：限定目录浅层检索（max_depth≤4、限定路径）秒级返回；全仓扫描风险未复测 | 检索（**仅限定浅层**） | [来源: docs/working/自主批次_路线图执行_20260913.md；R5v2 简报；本任务实测] |
| `file_read` / `file_write` / `list_directory` / `create_directory` | agentos-builtin-tools | 文件读写与目录管理 | ✅ 在册；本任务全程使用（写盘/读盘正常） | 三工位 | [来源: plugins/shared/tools/builtin_tools/plugin.json；本任务实测] |
| `memory` | memory_tool | 记忆存取（store/retrieve 等） | ✅ 在册（`default_profile.yaml` 显式启用）；运行态未在本任务内实测 | 三工位 | [来源: config/kernel/default_profile.yaml；plugins/shared/tools/memory/plugin.json] |
| `task_evaluate` | task_evaluate_tool | 任务评估（评估闸门） | ✅ 在册；本任务用于自评 | 三工位 | [来源: plugins/shared/tools/task_evaluate/plugin.json] |

## 二、已退役检索源（⛔ 禁止声明）

| 名称 | 状态 | 说明 |
|------|------|------|
| `mcp_registry_search` / `smithery_search` / `langchain_hub_search` / `external_resource_search` | ⛔ 已删除（2026-09-13 用户裁定） | 端点不可用（不讲 MCP 协议/404）+ 零白名单消费，四个插件已退役删除；新配置中不得声明这些工具名 |

[来源: docs/decisions/2026-09-13-orphan-mcp-search-plugins-removed.md]

## 三、目录中出现但未核验的检索类名称（本包不声明）

| 名称 | 出现位置 | 处理 |
|------|----------|------|
| `web_search` / `fetch` | `config/tools/builtin_tools_config.yaml` 工具目录（脚本自动生成） | 未核验为当前在册注册名（疑似历史名/别名），本包不声明；如需使用先核验注册面 |

[来源: config/tools/builtin_tools_config.yaml]

## 四、健康标注来源汇总

1. `docs/decisions/2026-09-13-orphan-mcp-search-plugins-removed.md` — 四源退役 + omnisearch 健康记录
2. `docs/working/自主批次_路线图执行_20260913.md` — enhanced_search 卡顿实测（15m13s）与 D1 修复立项
3. `config/kernel/default_profile.yaml` — 插件启用档案
4. 各插件 `plugin.json` — 工具声明面（名称/契约）
5. 本任务实测记录 — 限定浅层检索可用性、文件工具可用性
