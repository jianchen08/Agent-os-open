<!-- seed-version: 2 -->
# task_submit 三参数派发指令

你派发 task_submit 时，必须按下列规则核对 `workspace` / `inherit_from` / `isolation_level` 三个参数维度。表里查不到场景就用默认值。

## workspace（任务在哪个目录操作）
- **子任务一律不填**：系统自动继承父空间，填了会被忽略。
- **根任务**：
  - 要改/读某个已有目录 → 填该目录路径
  - 全新项目 / 纯新建文件 / 调研报告（不碰现有目录）→ 不填
  - 资源管理（创建/改 agent 或工具）→ 填 project_root
- **默认**：不填 = 空工作空间（或子任务继承父空间）。
- 禁止填系统内部路径 `container_xxx`，禁止给不碰现有文件的任务硬填。

## inherit_from（接续源任务的工作空间）
- 与已有任务**无关联** → 不传 inherit_from
- 要**接续源任务的产出/环境**继续干活 → `inherit_from="源任务ID"`：整份继承其工作空间坐标（目录/分支/项目根），新任务在同一工作树干活，上下文重新开始
- **与 workspace 互斥**：同给即报错，接续只走 inherit_from，不要用 workspace 表达接续
- **恢复失败/超时/同任务续作 → 一律先 `task_manage(action="continue", task_id="原任务ID", message=...)`**：同一管道、同一 run、上下文与工作空间全保留（running 注入 / stopped 恢复 / failed·timeout 重试 / completed+message 返工重开）。回归审查、验证复跑、同一任务链的下一轮都走 continue，不新开任务
- **仅以下情况才用 `inherit_from` 新任务**：换执行 agent（提示词不同）、目标换到与原上下文无关的新工作、原任务上下文已不可用。新任务上下文从零开始，禁止用它承载同任务续作
- **已完成任务返工或验收标准变更** → `task_manage(action="continue", task_id=..., message=..., acceptance_criteria={...})`：message 必填；`acceptance_criteria` 传即整组覆盖验收指标并清零评估进度（task.evaluation/重试计数），旧判定作废
- **铁律**：恢复失败任务**禁止裸提交**（不带继承会丢弃原工作空间）；如确需裸提交，先 human_interaction 报人类确认。
- **默认**：不传 = 全新任务。
- **跨 agent 不继承**：不同 agent 有不同系统提示词，接续前判断源工作空间对新执行者是否有用。

## isolation_level（是否在隔离副本操作）
- **只有根任务填，子任务一律不填**：源码对子任务的 isolation_level 一律继承父级，你填了无效。
- **根任务**：绝大多数不填（默认 `isolated`，在副本操作最安全）；仅资源管理任务填 `non_isolated`（直接操作系统内部路径）。
- **`host` 是错误值**：枚举只有 `non_isolated` / `isolated`，直接操作对应 `non_isolated`。

## 模式编排（任务上下文带 mode 键时）
- 任务上下文**带 `mode` 键** → 优先从该模式附带的**编排清单**中选匹配任务型的编排派单（用其编排键指定 pipeline）；编排清单与路由指引见上下文模式段（模式物料）。
- **不带 `mode` 键** → 按既有派单规则（上文各条），编排走共享默认。

## 派发前默念三句
1. workspace：根任务按场景填目标目录，子任务一律不填，纯新建不填。
2. inherit_from：只在换 agent/换目标时用；同任务续作一律 task_manage(continue)；与 workspace 互斥，恢复失败禁止裸提交。
3. isolation_level：只有根任务填，默认不填，资源管理才填 non_isolated，子任务永不填。
