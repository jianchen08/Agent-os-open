/**
 * 新建根任务表单（widget 化 T12：原 CreateTaskModal 311 行并入 FormWidget modal 壳）
 *
 * 静态选项走 fields 声明（词汇表）；动态选项（父容器/执行 Agent）由宿主从
 * store/端点注入；task_submit 参数矩阵联动（父容器→子任务 non_container、
 * 容器任务无 agent/拓扑/隔离）转为提交时派生，落在宿主 onSubmit。
 */
import { useEffect, useMemo, useState } from 'react'
import { toast } from '@/components/ui/sonner'
import { FormWidget } from './FormWidget'
import { useAgentStore } from '@/stores/agentStore'
import { createRootTask, getContainerTasks } from '@/services/api/tasks'
import type { UIInputFormField } from '@/types/schema'

const WORKSPACE_MODES = [
  { value: '', label: '默认（worktree）' },
  { value: 'worktree', label: 'worktree（隔离副本，不影响原项目）' },
  { value: 'plain', label: 'plain（直接操作目标目录）' },
]

const ISOLATION_LEVELS = [
  { value: '', label: '默认（隔离）' },
  { value: 'isolated', label: '隔离' },
  { value: 'non_isolated', label: '非隔离' },
]

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
  const agents = useAgentStore((state) => state.agents)
  const [containers, setContainers] = useState<Array<{ id: string; title: string }>>([])

  useEffect(() => {
    if (isOpen && sessionId) {
      getContainerTasks(sessionId)
        .then(setContainers)
        .catch(() => setContainers([]))
    }
  }, [isOpen, sessionId])

  const fields: UIInputFormField[] = useMemo(
    () => [
      { name: 'title', type: 'input', label: '标题', required: true, placeholder: '输入任务标题...' },
      { name: 'description', type: 'textarea', label: '描述（可选）', placeholder: '任务详细描述...' },
      ...(containers.length > 0
        ? [
            {
              name: 'parent_task_id',
              type: 'select' as const,
              label: '父容器（可选，选则挂为子任务，工作空间继承）',
              options: [
                { label: '无（创建根任务）', value: '' },
                ...containers.map((c) => ({ label: c.title || c.id, value: c.id })),
              ],
            },
          ]
        : []),
      {
        name: 'task_scope',
        type: 'select',
        label: '任务类型',
        default: 'non_container',
        options: [
          { label: '非容器（直接执行）', value: 'non_container' },
          { label: '容器（工作空间集合）', value: 'container' },
        ],
        description: '选了父容器则为非容器子任务',
      },
      {
        name: 'target_id',
        type: 'select',
        label: '执行 Agent（非容器必填）',
        options: agents
          .filter((a) => a.status === 'active')
          .map((a) => ({ label: a.name, value: a.configId || a.id })),
        description: '容器任务不直接执行，无需选择',
      },
      { name: 'workspace', type: 'input', label: '工作空间（可选）', placeholder: '留空使用默认工作空间...' },
      {
        name: 'workspace_mode',
        type: 'select',
        label: '工作空间拓扑',
        options: WORKSPACE_MODES,
        description: '容器任务恒为隔离复制，此选项不生效',
      },
      { name: 'isolation_level', type: 'select', label: '隔离模式', options: ISOLATION_LEVELS },
    ],
    [agents, containers],
  )

  return (
    <FormWidget
      modal={{ title: '新建任务' }}
      open={isOpen}
      onClose={onClose}
      fields={fields}
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
