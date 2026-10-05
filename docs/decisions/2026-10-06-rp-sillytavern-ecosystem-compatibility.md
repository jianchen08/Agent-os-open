# ADR 2026-10-06：角色扮演子系统 = 「酒馆（SillyTavern）增强」战略

## 背景

调研 SillyTavern（中文俗称「酒馆」）、RisuAI、afengy.com、character.ai、JanitorAI、SpicyChat、Convai、Inworld、Hiddendoor、NovelAI、筑梦岛等 RP 平台后，发现：

- **SillyTavern（酒馆）是全球最大的 RP 前端生态**——V2/V3 PNG 角色卡是事实标准；STscript、Regex、Lorebook、Slash Commands 已被社区广泛使用
- 用户群最大（GitHub 30K+ star，Reddit 90K+ 关注）
- 创作者积累了大量 UGC（百万级角色卡）
- 它的生态不是单一产品，是**事实标准 + 社区习惯**的组合

之前的 ADR（`2026-10-06-rp-two-axis-mode-spectrum.md`）提出"二轴模式谱"，解决了"LLM/规则复杂度"轴的灵活性，但**没有回答：与最大生态 SillyTavern 的关系是什么**。

如果 AgentOS 强行做"封闭生态"，将面临：
- 用户迁移成本高（百万 ST 用户）
- UGC 流失（创作者不愿重新做）
- 与 ST 事实标准对抗

如果 AgentOS 完全复刻 ST，则失去差异化。

## 决策

**AgentOS 角色扮演子系统的核心定位 = 「酒馆（SillyTavern）增强」**。

具体含义：

1. **完全兼容 ST 资产**：
   - V2/V3 PNG 角色卡双向导入导出（PNG tEXt chunk 读写）
   - STscript 直接执行
   - ST Regex Scripts 兼容执行
   - ST World Info / Lorebook 完整兼容
   - ST Slash Commands 识别（`/set`, `/let`, `/add`, `/trigger` 等）
   - ST Variables 兼容 + 提升（强 schema 校验）
   - ST Extension 通过 compat 层加载
   - ST Group Chat 兼容

2. **补齐 ST 缺的部分**（差异化）：
   - **状态机 schema 化** —— ST Variables 由 STscript 维护（弱类型），AgentOS 提供强 schema + 内核 SQLite 持久化
   - **叙事图** —— ST 用 regex trigger（粗糙），AgentOS 提供节点 + 决策 + 触发器的可视化系统
   - **规则引擎** —— ST 无战斗/事件确定性计算，AgentOS 提供
   - **结构化输出契约** —— ST LLM 输出纯文本，AgentOS 提供 4 档契约 + 「宽进严出」解析器
   - **模式谱** —— ST 必须有 LLM，AgentOS 提供"无 LLM = visual novel"
   - **本地嵌入检索** —— ST Data Bank 依赖云端，AgentOS 提供本地稳定嵌入
   - **AgentOS 内核生态** —— 持久化、agent_config_load、MCP、Multi-agent、task system

3. **渐进增强路径**：用户**不必一次性迁移**，而是按需升级：
   - 阶段 0：导入 ST 卡，与 ST 100% 一致
   - 阶段 1：开启 schema（HP/位置/时间不漂移）
   - 阶段 2：开启叙事图（分支不再漂移）
   - 阶段 3：开启规则引擎（战斗确定性）
   - 阶段 4：完全 AgentOS 原生（性能 + MCP）

4. **承诺**：
   - **零迁移成本**：ST 角色卡 = AgentOS 角色卡
   - **可逆迁移**：导出回 ST 仍可用
   - **保留 prompt 习惯**：ST 创作者的 prompt / Mod 继续生效
   - **保留脚本**：STscript 直接运行

## Alternatives Considered

### A. 完全独立生态

- 优点：可自由设计
- 缺点：与全球最大 RP 生态脱节，迁移成本极高，无 ST 用户基础
- 否决理由：UUGC（user-generated content）冷启动难

### B. 完全复刻 ST

- 优点：兼容性好
- 缺点：差异化差；ST 本身有缺陷（弱 schema），复刻 = 继承缺陷
- 否决理由：没有护城河

### C. 与 ST 并行（同质竞品）

- 优点：可竞争
- 缺点：用户会选更"全"的那个，资源分散
- 否决理由：与 ST 抢用户没有意义

### D. ST 资产兼容 + ST 缺的部分补齐 = 「酒馆增强」（采纳）

- 优点：
  - 复用 ST 百万级 UGC（角色卡）
  - 复用 ST 用户社区习惯（prompt、STscript、Mod）
  - 复用 ST 内部生态（extension、regex）
  - 与 ST 共赢
  - 差异化清晰（schema、叙事图、规则引擎、模式谱）
- 缺点：
  - 长期需要维护 ST compat 层
  - ST 升级（V4 等）时需要适配
- 缓解：
  - compat 层放在 `agentos.rp.st_compat` 插件，与核心解耦
  - ST 升级时只改 compat 插件，核心架构不变

## 影响

### 正面

- **冷启动**：ST 用户 100% 可迁入
- **UGC**：ST 角色卡直接可用（百万级）
- **社区协作**：可与 ST 社区合作（赞助 / 文档同步 / Discord 互通）
- **差异化清晰**：补 ST 缺的部分，建立"ST+AgentOS 比 ST 单独更好"的价值主张

### 负面

- **必须维护 ST compat 层**：需要持续跟进 ST 升级
- **PNG 解析器有技术风险**：PNG tEXt chunk 是 de facto 标准，但实现需细致
- **STscript 是 JS，需要 QuickJS 运行时**：增加依赖

### 中性

- 既有 agent_config_load 路线仍可作为「纯 LLM 聊天模式」存在，不冲突
- 与 afengy.com 的关系保持（中文生态互补）

## 依赖与解锁

依赖：M1 的核心 cell 设计（已写 `2026-10-06-rp-two-axis-mode-spectrum.md`）

解锁以下 ADR（按 M0 顺序）：
- PNG tEXt chunk 读写实现
- ST 内部 API stub 清单（~30+ API）
- STscript 解释器语法子集
- Regex 兼容层
- World Info / Lorebook 兼容层
- ST Extension compat 加载策略

## 关键承诺（对 ST 用户）

1. **零迁移成本** —— 导入 ST 角色卡 = 在 AgentOS 中使用
2. **可逆迁移** —— 导出回 ST 仍可用
3. **增量增强** —— 一项项加引擎功能，不强推
4. **保留 prompt 习惯** —— ST 创作者的 prompt / Mod 继续生效
5. **保留脚本** —— STscript 直接运行
6. **ST 插件继续工作** —— 通过 compat 层加载

## 用户路径示例

```
Week 0：ST 用户有 100 个角色卡（PNG）
Week 1：用户安装 AgentOS
         运行 `agentos rp import *.png`
         → 100 个 YAML 角色卡
Week 2：用户选择 5 个最爱角色，运行 `agentos rp upgrade`
         → 自动推断 schema + 建议增强
         → 用户接受 3 个角色的 schema 增强
Week 3：用户在新角色卡上写 YAML
         → 享受：状态机 schema、叙事图、模式谱
Week 4：用户导出回 ST
         → ST 用户社区看到"AAA-增强版"
         → 新用户开始尝试 AgentOS
```

## 实施优先

| 阶段 | 内容 | 用户群 | 优先级 |
|---|---|---|---|
| **M0**（2 周） | **SillyTavern 兼容** | **ST 用户（最大潜在群体）** | ★★★★★ |
| M1（3 周） | `core + with_llm` | 80% 创作者 | ★★★★★ |
| M2（2 周） | `core + without_llm` | 离线 / 视觉小说 | ★★★★ |
| M3（3 周） | `full + with_llm` | 复杂 RP | ★★★ |
| M4（2 周） | 生态迁移 + 市场 | 高级 + 老用户 | ★★ |

**关键路径**：**M0 不做完不开始 M1**。M0 是地基，没有它 M1+ 是无源之水。

## 归档

- 决策日期：2026-10-06
- 调研输入：SillyTavern GitHub / Docs / Reddit；社区反馈；与 afengy/character.ai/Convai 对比
- 主要产出文档：
  - `docs/working/rp-game-engine-with-llm-enhancement.md`（§1.5 新增章节）
  - 本 ADR
- 状态：**已采纳**，进入实施路线 Phase 0（M0）