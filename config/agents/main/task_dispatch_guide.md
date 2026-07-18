# task_submit 三参数派发指令

你派发 task_submit 时，必须按下列规则核对 `workspace` / `inherit` / `isolation_level` 三个参数。表里查不到场景就用默认值。

## workspace（任务在哪个目录操作）
- **子任务一律不填**：系统自动继承父空间，填了会被忽略。
- **根任务**：
  - 要改/读某个已有目录 → 填该目录路径
  - 全新项目 / 纯新建文件 / 调研报告（不碰现有目录）→ 不填
  - 资源管理（创建/改 agent 或工具）→ 填 project_root
- **默认**：不填 = 空工作空间（或子任务继承父空间）。
- 禁止填系统内部路径 `container_xxx`，禁止给不碰现有文件的任务硬填。

## inherit（是否复用源任务的上下文/产出）
- 与已有任务**无关联** → 不传 inherit
- 要**接着源任务对话**（改了目标，上下文还要用）→ `mode="pipe"`
- 要**复用源任务产出文件**（换方案，文件要留着）→ `mode="workspace"`
- **两者都要**（最常见延续）→ `mode=["pipe","workspace"]`
- 恢复失败**有产出** → `pipe+workspace`，goal 指出问题定向修复
- 恢复失败**无产出** → `workspace`，至少保住环境
- **超时** → 用 `task_manage(action="continue")`，不用 task_submit
- **铁律**：恢复失败任务**禁止裸提交**（不带继承会丢弃原管道和上下文）；如确需裸提交，先 human_interaction 报人类确认。
- **默认**：不传 = 全新任务。
- **跨 agent 不继承 pipe**：不同 agent 有不同系统提示词，继承另一个 agent 的对话历史无意义。继承前判断上下文对新执行者是否有用。

## isolation_level（是否在隔离副本操作）
- **只有根任务填，子任务一律不填**：源码对子任务的 isolation_level 会强制清除（容器直接子任务）或继承父级（其他子任务），你填了无效。
- **根任务**：绝大多数不填（默认 `isolated`，在副本操作最安全）；仅资源管理任务填 `non_isolated`（直接操作系统内部路径）。
- **`host` 是错误值**：枚举只有 `non_isolated` / `isolated`，直接操作对应 `non_isolated`。

## 派发前默念三句
1. workspace：根任务按场景填目标目录，子任务一律不填，纯新建不填。
2. inherit：有产出延续才继承，恢复失败禁止裸提交。
3. isolation_level：只有根任务填，默认不填，资源管理才填 non_isolated，子任务永不填。
