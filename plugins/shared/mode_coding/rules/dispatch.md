本段约束编码模式下主 agent 的任务派发（经 mode_material_inject 模式物料步骤注入：本包 material.py 组装器把本文件整文追加进 system prompt 尾部）。

- 派发实现/修复类任务时，goal 必须写清改动面（要动的文件或模块路径）与验收判据；task_submit 必须声明验收指标——产物类用 `file_check`（`input_params.path` = goal 声明的产物文件相对路径），可命令化验证的用 `command`（退出码 0 = 通过）。
- 完成判据指向交付物内容：写成「<目标文件> 里存在 <可验证内容>」（某条目/某测试文件在位/某命令通过），不写「落盘报告即完成」——后者会让执行者只交仪式性台账而无实质改动。
- 接续同一任务链用 `task_manage(action="continue", task_id=...)`，不新开任务；仅换执行 agent 或换目标才 `task_submit` + `inherit_from`。
- 代码审查与功能验证外包给独立下级（`code_reviewer_agent` / `function_verifier_agent`），不得自审自验；回归复验用 continue 保留同一管道。
- 签发的验收测试对执行方只读：禁改断言、禁跳过用例、禁调整预期值；需要改判据时停下回报，不以让门禁变绿为目标反向改判据。
- 探索/调研类子任务在任务书显式标注「探索」，探索产物显式标记、不混入生产路径。
