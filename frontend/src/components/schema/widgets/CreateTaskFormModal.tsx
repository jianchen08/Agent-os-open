/**
 * 新建根任务表单（widget 化 T12：原 CreateTaskModal 311 行并入 FormWidget modal 壳）
 *
 * 字段声明后移：表单字段由 task_form 服务插件在 config/task_form.yaml 声明、
 * 经 fieldsUri=task_form 插件 form 拉取渲染（FormWidget datasource 模式自取）；
 * 动态选项（挂靠项目/执行 Agent）由字段声明的 datasourceUri 指向内核数据源端点，
 * 前端按填写的值自行去内核取对应数据。
 *
 * 本组件只保留提交语义（createRootTask：project_id 可选挂靠，任务必选执行 Agent
 * ——对齐 task_submit 参数矩阵）。
 */
import { useMemo } from 'react'
import { TASK_FORM_ENDPOINTS } from '@/services/api/endpoints.generated'
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
  /** 字段声明来自 task_form 服务（表单配置的唯一真相源） */
  const fieldsUri = useMemo(() => TASK_FORM_ENDPOINTS.task_form_get, [])

  return (
    <FormWidget
      modal={{ title: '新建任务' }}
      open={isOpen}
      onClose={onClose}
      fieldsUri={fieldsUri}
      submitLabel="创建"
      onSubmit={async (values: Record<string, unknown>) => {
        if (!sessionId) throw new Error('缺少会话上下文')
        const targetId = String(values.target_id ?? '').trim()
        if (!targetId) throw new Error('必须选择执行 Agent')
        const workspaceMode =
          values.workspace_mode === 'plain' || values.workspace_mode === 'worktree'
            ? values.workspace_mode
            : ''
        const isolationLevel =
          values.isolation_level === 'isolated' || values.isolation_level === 'non_isolated'
            ? values.isolation_level
            : ''
        const projectId = String(values.project_id ?? '').trim()
        await createRootTask({
          title: String(values.title ?? '').trim(),
          description: String(values.description ?? '').trim(),
          project_id: projectId || undefined,
          target_id: targetId,
          workspace: String(values.workspace ?? '').trim(),
          workspace_mode: workspaceMode,
          isolation_level: isolationLevel,
          thread_id: sessionId,
        })
        toast.success(projectId ? '任务已创建并挂靠项目，开始执行' : '任务已创建并开始执行')
        onCreated()
      }}
    />
  )
}
