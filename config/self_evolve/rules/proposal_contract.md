# 提案契约（proposal_contract）—— 提案 Schema、七条校验与按对象的修改方式

> B 部类物料（governance）：只允许用户修改，进化 agent 只读。
> 可改面/冻结面的权威清单见 material_registry.md。

## 一、ChangeProposal 结构（MVP：整文件替换语义）

| 字段 | 必填 | 说明 |
|---|---|---|
| layer | ✓ | L1（提示词）/ L2（配置）/ L3（新增插件） |
| target | ✓ | 主仓相对路径（A 部类可改面内） |
| change_type | ✓ | param_edit / content_edit / list_add / list_remove / new_plugin |
| content | ✓ | 提案后的完整文件内容 |
| motivation | ✓ | 症状证据（引用签名簇）+ 可证伪假设 |
| effect_probe | ✓ | 生效性探针：运行时如何证明改动生效（state 键 / 日志行 / counter） |
| base_hash | — | 基于的主仓当前文件 sha256（file_read 后自算；新文件豁免） |

## 二、七条静态校验（proposal_submit 自动执行，不过即拒）

1. **schema 完整**：必填字段齐全、change_type 合法。
2. **冻结面**：B 部类路径 + 安全红线文件——触碰即拒（清单见 material_registry.md，代码同步于 proposal.py `FROZEN_PREFIXES`）。
3. **路径白名单**：仅 material_registry.md A 部类前缀（proposal.py `EVOLVABLE_PREFIXES`）。
4. **原子性**：一次一个文件、一个维度；禁止多文件捆绑。
5. **base 一致性**：目标文件已前移（base_hash 不符）即拒——必须基于最新内容重新生成。
6. **写面唯一**：content 不得内嵌绝对路径；落盘只进 staging。
7. **题面相似度**：提案内容不得复现评测题面（MVP 记录 skipped，二期嵌入检索强制）。

被拒后按违规清单逐条修正重提；**禁止原样重试**；同 target 被拒 2 次必须换假设或放弃该杠杆。

## 三、按修改对象的修改方式（统合系统既有机制）

### A. 提示词文件（L1 · content_edit）

- **对象**：persona 文件、业务规则文件（`config/rules/` 下非安全类）、`task_dispatch_guide.md`。
- **方式**：整文件替换；**必须保持 `{{path:...}}` 引用与模板变量（`{{...}}`）完整**——引用清单是 agent 配置的装配骨架，删引用等于删能力。
- **红线**：安全红线文件（information_integrity_rules / document_context_rules）冻结；不新增未经用户确认的规则文件。

### B. agent 配置（L2 · param_edit / list_edit）

- **对象**：`config/agents/*/*.yaml` 的参数面——`tool_ids`（白名单增删）、`model_tier`、`default_params`、thinking 配置。
- **方式**：只改参数字段；`config_id`/`name`/`agent_type`/`level` 等身份字段冻结。
- **机制对齐**：tool_ids 增删必须指向**已注册工具**（内核 tools 清单可查）；需要新工具面时优先派生新 agent 配置（不改共享 agent），与"模式选 agent 而非覆盖 agent"的裁定一致。

### C. 管道配置（L2 · param_edit）

- **对象**：`config/pipelines/*.yaml` 的步骤参数、`when` 条件、`set` 路由参数。
- **方式**：只调参数与开关；**步骤 id 结构与编排拓扑的调整需人工**（防止复制漂移——管道是单一真值，模式通过 pipeline_profile.params 注入分化，见方案 §15.2）。

### D. 插件 manifest 参数（L2 · param_edit）

- **对象**：`plugins/shared/**/plugin.json` 的 `fields` 阈值参数（压缩触发比例、duplicate_check 阈值、熔断 M 值等）。
- **方式**：只改 `fields.*.default` 值；`id`/`entry`/`capabilities` 结构冻结。改参数必须同步核对消费面（该 fields 被哪个插件读取、生效链路是什么）——生效性探针按此写。

### E. 新增插件（L3 · new_plugin）

- **方式**：命名空间 `plugins/shared/{tools,pipeline}/auto_gen_*`；交付四件套 = plugin.json（**含 output_schema + render**，tool_core 契约 fail-closed 校验）+ Python 实现 + pytest 测试 + tool_ids 登记（加入对应 agent 的白名单，走 B 类提案）。
- **边界**：不修改任何现有共享插件源码；新插件 `dependencies` 需声明依赖的既有插件；晋升前 G2 校验必须过。

### F. 禁止的修改方式（无论对象）

- 在 goal/提示词里写路径或工作空间（工作空间是系统机制：任务默认 `isolated` 自动分配，指定走 `task_submit` 的 `workspace` 参数——反例案例一）；
- 多文件捆绑、多维度混合（归因灾难）；
- 以"通过某单一 case"为目的的针对性行为（防过拟合纪律，见 evolution_rules.md）。
