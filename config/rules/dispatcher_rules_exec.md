<!-- seed-version: 4 -->
派发纪律（派发/回归/失败时按此核对）：

派发前，检查 goal：
- 只写目标和背景，不塞步骤/工具/流程
- 只传文件路径，不传内容

派发后：等系统通知再用 task_manage 查看，不立即查

接续已有任务 → 同一任务链一律先 `task_manage(action="continue", task_id="原任务ID", message=...)`：同一管道、同一 run、上下文与工作空间全保留（running 注入 / stopped 恢复 / failed·timeout 重试 / completed+message 返工重开）；验收标准变更可带 `acceptance_criteria` 整组覆盖。仅换执行 agent、目标换到与原上下文无关的新工作、或原上下文已不可用时，才 `task_submit` + `inherit_from="源任务ID"`：只继承工作空间坐标（目录/分支/项目根），新任务上下文重新开始；与 workspace 互斥，不要同给。

回归时（外包未通过）：
- 自己修 Must Fix：重载技能修复并自测，不继承
- 重派外包回归：`task_manage(action="continue", task_id=原任务ID, message="复验上轮 Must Fix + 增量审本次修复范围")`；验收指标变更带 `acceptance_criteria` 整组覆盖。仅换 agent/换目标才 `task_submit` + `inherit_from`
- 各环节最多 3 轮，超过升级到架构审查或 human_interaction

恢复失败/超时——禁止裸提交（铁律）：
- 超时/环境类失败（timeout/model_unavailable/llm_fail_loop）→ `task_manage(action="continue")` 原地重试同一管道，不用 task_submit
- 同因 continue 3 次仍败，或方向性失败确需换 agent/目标 → 才 `inherit_from` 重派；确需裸提交先 human_interaction 报人类确认
- 同一问题最多 3 次恢复，3 次仍失败上报上级

任务范围红线：名称与范围以上级原始描述为准，禁止擅自改名或改写（拆分/合并/简化/重命名），要改需上报上级；出错不许假设"任务太大"而缩水削减范围，多次失败需请求人类。
