/**
 * 新建根任务表单（widget 化 T12：原 CreateTaskModal 311 行并入 FormWidget modal 壳）
 *
 * 字段声明后移：表单字段由 task_form 服务插件在 config/task_form.yaml 声明、
 * 经 fieldsUri=/ext/task_form/form 拉取渲染（FormWidget datasource 模式自取）；
 * 动态选项（父容器/执行 Agent）由字段声明的 datasourceUri 指向内核数据源端点，
 * 前端按填写的值自行去内核取对应数据（dependsOn 值变化自动重拉）。
 *
 * 本组件只保留提交语义（createRootTask 派发矩阵：父容器→子任务 non_container、
 * 容器任务无 agent/拓扑/隔离——对齐 task_submit 参数矩阵派生）。
 */
import { useMemo } from 'react'
import { toast } from '@/components/ui/sonner'
import { FormWidget } from './FormWidget'
import { createRootTask } from '@/services/api/tasks'

export function CreateTaskFormModal({
  isOpen,
  onClose,
  sessionId,
  onCreated,
}: {
  isOpen: boolean
  onClose: () => void
  sessionId: string
  onCreated: () => void
}) {
  /** 字段声明来自 task_form 服务（表单配置的唯一真相源）；session 内嵌以限定容器选项 */
  const fieldsUri = useMemo(
    () =>
      `/ext/task_form/form${sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''}`,
    [sessionId],
  )

  return (
    <FormWidget
      modal={{ title: '新建任务' }}
      open={isOpen}
      onClose={onClose}
      fieldsUri={fieldsUri}
      submitLabel="创建"
      onSubmit={async (values: Record<string, unknown>) => {
        if (!sessionId) throw new Error('缺少会话上下文')
        // 参数矩阵派生（对齐 task_submit / 原 CreateTaskModal 联动）：
        // 父容器 → 子任务 non_container + workspace 继承；容器任务无 agent/拓扑/隔离
        const parent = String(values.parent_task_id ?? '').trim()
        const isChild = parent !== ''
        const scope: 'container' | 'non_container' = isChild
          ? 'non_container'
          : values.task_scope === 'container'
            ? 'container'
            : 'non_container'
        const isContainer = scope === 'container'
        const targetId = String(values.target_id ?? '').trim()
        if (!isContainer && !targetId) throw new Error('非容器任务必须选择执行 Agent')
        const workspaceMode =
          values.workspace_mode === 'plain' || values.workspace_mode === 'worktree'
            ? values.workspace_mode
            : ''
        const isolationLevel =
          values.isolation_level === 'isolated' || values.isolation_level === 'non_isolated'
            ? values.isolation_level
            : ''
        await createRootTask({
          title: String(values.title ?? '').trim(),
          description: String(values.description ?? '').trim(),
          task_scope: scope,
          target_id: scope === 'non_container' ? targetId : '',
          workspace: String(values.workspace ?? '').trim(),
          workspace_mode: isContainer ? '' : workspaceMode,
          isolation_level: isContainer ? '' : isolationLevel,
          thread_id: sessionId,
          parent_task_id: parent || undefined,
        })
        toast.success(isChild ? '子任务已创建，工作空间继承父容器' : isContainer ? '工作空间已创建' : '任务已创建并开始执行')
        onCreated()
      }}
    />
  )
}
