---
name: ACP 额度池
description: 把编码任务派发给本机多个编码 agent（OpenCode/ZCode 等）白嫖额度池——统一派发脚本（acp/oneshot 双后端）+ per-provider 额度记账。需要把任务派给外部编码 agent、查各家额度记账面、或按任务分级选家时加载。技能以插件载荷播种（plugins/shared/acp_quota_pool）。
---

# ACP 额度池（acp-quota-pool）

把能白嫖的编码 agent 额度收进一个池子：外部发起方统一派发、一份记账面看各家用量。
决策记录：`docs/decisions/2026-10-15-acp-agent-connector-quota-pool.md`；
方案与 provider 矩阵真值：`docs/working/ACP额度池连接器_方案_20261015.md`。

## 选型三规则（路由真源）

1. **性能基础线**：接活 provider 的模型强度 **≥ GLM-5.3-Flash**（`strength=baseline`）。
   `strength=weak/unverified` 一律视作不达线。
2. **选型优先序**：免费 > 低消耗 > 付费；同级看健康度（账本撞墙信号位，撞墙家本轮降级）。
3. **任务分级决定可选池**（发起方判定，派发前先定级）：

| 级别 | 任务形态 | 可选池 |
|---|---|---|
| **S0** 琐碎 | 问答、单文件小改、机械替换、格式化 | 全池（free 优先，weak 可接） |
| **S1** 常规 | 模块级实现/修复/补测试/文档 | baseline 线以上优先；池内无 baseline 家时可落到 unverified 家并如实告知 |
| **S2** 关键 | 架构/跨模块/难回归/生产面 | **仅 strength=baseline 家；weak/unverified 禁接** |

## provider 定档表（2026-10-16 M1 更新，随 M1.5/M2 实测更新）

| Provider | cost_class | strength | ACP 入口 | 额度模型 | 批次 |
|---|---|---|---|---|---|
| opencode | free | unverified（免费档模型未对标，S2 禁接） | `opencode acp`（原生，本机 1.18.18 已实证） | Zen 免费模型档（按模型计费） | **M0 现役** |
| zcode | paid（GLM coding plan，1.5x 促销） | unverified（基准题定档前，S2 禁接执法照旧） | 无原生 ACP；one-shot `zcode -p <goal> --json`（M1 接入，`--mode yolo` 免审批，无会话续做） | 官方 glm-plan-usage 额度面（M1.5 接账本）；无头实际落 GLM-5.3（standalone 目录限制，上游 zai-org/feedback#744） | **M1 现役** |
| gemini CLI | free（个人号 1000 请求/天） | baseline（对标后改档） | `gemini --experimental-acp` | 个人 Google 账号日额度 | M2 |
| codebuddy / trae | free 有限额 | 未验 | 无 ACP → 适配器 | 国内免费每日/每月额度 | M2 |

定档变更 = 改 `providers.toml`（spawn/记账面）+ 本表（路由口径）同刀。

## 调用方式（工作空间内相对寻址，bash 直达）

```bash
# 派发（池内多家，--provider 必填显式指定；zcode 为 one-shot，--session-id 不适用）
python skills/acp-quota-pool/scripts/acp_pool.py dispatch \
  --goal "目标全文一次写全：目标+背景+验收判据+结果落盘路径" \
  --cwd <任务工作空间> --provider opencode [--session-id <续做会话>] --json
python skills/acp-quota-pool/scripts/acp_pool.py dispatch \
  --goal "..." --cwd <任务工作空间> --provider zcode --json

# 记账面报表（派发计数/最近会话/撞墙信号位；v0 只报记账，无主动查询）
python skills/acp-quota-pool/scripts/acp_pool.py report
```

- 依赖缺省可用 `uv run --with agent-client-protocol==0.12.1 python skills/acp-quota-pool/scripts/acp_pool.py ...`。
- 退出码：0=ok；1=派发 failed（看 summary 与账本撞墙归类）；2=配置/环境错误。
- 账本落用户空间 `<USER_ROOT>/data/acp_bridge/quota_ledger.json`（运行态数据，可重建）。
- 会话续做：把返回的 `session_id` 传回 `--session-id`（仅 ACP 家；zcode one-shot 无会话）。

## 免审批要点

- **opencode（ACP）**：许可请求由派发引擎代答 `allow_once`（cwd 即本次授权边界，
  对齐"隔离容器即安全边界"口径）。S2 任务勿把 cwd 指向生产面；高危写操作仍应在
  goal 里显式禁止或由发起方先行确认。
- **zcode（one-shot）**：`-p` 无交互审批面，注册默认 `--mode yolo` 即免审批；
  只读验证任务用 `--mode plan`（注册表存 yolo，冒烟测试用 plan）。cwd 即授权
  边界，口径同上。
- 各家 no-confirm 旗标（非 ACP 通道备用）：opencode `run --auto`（危险，绕权限）；
  gemini CLI `--yolo` [M2 待验]。
- 撞墙信号（quota/rate-limit、auth）入账本后，本轮选型自动降级下一家；不依赖静态档位。

## 已知边界（2026-10-16）

- 池内现役两家：opencode（ACP，有会话）+ zcode（one-shot，无会话）；路由策略
  （自动选家/换家重试）M2 接线，多家属显式指定制（`--provider` 必填）。
- `agent-client-protocol` 0.12.1 客户端序列化有判别字段违约（引擎内已按线缆规范
  补丁，升级 ≥1.0 复验）。
- 账本 v0 = 被动记账（派发计数/撞墙归类），主动额度查询面 M1.5 起（glm-plan-usage）。
- zcode 无头（`-p`）实际落 GLM-5.3：standalone 目录只取首个 builtinModelIds，
  GLM-5.3-Flash 不可无头选（上游 zai-org/feedback#744 在案，修后复验改档）。
