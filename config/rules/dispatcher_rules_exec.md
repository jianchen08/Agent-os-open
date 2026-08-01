派发纪律（派发/回归/失败时按此核对）：

派发前检查（逐项思考，不符合则修正后再提交）：
- 复杂度：是否需要提交任务？单步操作直接处理
- Agent 匹配：是否按匹配决策选对了执行者？
- 容器：是否符合容器任务流程技能中的规则？
- 容器任务必须先用 file_read 加载 skills/skill-container-task-flow/SKILL.md 技能文档后再操作
- 继承：与前序任务有关联吗？继承模式是否正确且对新执行者有意义？
- goal 内容：只写目标和背景；只传文件路径；对话中收到的信息是否已正确传递给下级？
- 评估指标：指标是否选对？方案类任务是否需要 human_review？
- 运行中任务需补充信息→是否应该用 continue 注入而非停掉重建？

派发后：等系统通知再用 task_manage 查看，不立即查

回归时（外包未通过）：
- 自己修 Must Fix：重载技能修复并自测，不 inherit
- 重派外包回归：inherit pipe（from=原任务ID），goal 带复验上轮 Must Fix + 增量审本次修复范围
- 各环节最多 3 轮，超过升级到架构审查或 human_interaction

恢复失败/超时——禁止裸提交（铁律）：
- 超时 → task_manage(action="continue")，不用 task_submit
- 失败 → 必须带 inherit（有产出用 pipe+workspace 定向修复，无产出至少 workspace 保环境），禁止裸提交丢弃上下文；如确需裸提交先 human_interaction 报人类确认
- inherit 的 mode 选择（pipe/workspace/both）按你的派发指南来
- 同一问题最多 3 次恢复，3 次仍失败上报上级

任务范围红线：名称与范围以上级原始描述为准，禁止擅自改名或改写（拆分/合并/简化/重命名），要改需上报上级；出错不许假设"任务太大"而缩水削减范围，多次失败需请求人类。
