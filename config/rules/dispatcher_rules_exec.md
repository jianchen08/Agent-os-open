派发检查（派发/回归/失败时按此核对）：

派发给 L3 前，检查 goal：
- 只写目标和背景，不塞执行步骤、工具选择、流程顺序
- 只传文件路径，不传文件内容
- 需要看上轮过程/报告/产出的（回归、补验证、延续）用 inherit；继承管道（mode=pipe）看对话历史，继承工作空间（mode=workspace）在原产出上改，两者都需就都设；无关联提交无继承新任务

派发后：等系统通知再用 task_manage 查看，不立即查

回归时（外包环节未通过）：
- 自己修 Must Fix：重新加载对应技能修复，自己跑测试确认，不 inherit
- 重派外包回归：inherit pipe（from=原任务ID），goal 带复验上轮 Must Fix + 增量审本次修复范围
- 各环节最多 3 轮，超过升级到架构审查或 human_interaction

失败时：
- 先用 task_manage(action="get") 查失败原因，检查 worktree 有没有产出
- 有产出：inherit 原任务定向修复（goal 指出具体问题），不从零重做
- 无产出或完全错误：才重新提交（inherit_workspace_from 保留环境）
- 同一问题最多 3 次，3 次失败上报上级
