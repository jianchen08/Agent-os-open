/**
 * 新建根任务模态框
 *
 * 用户以 L1 身份手动创建根任务（等价于 L1 主 agent 调 task_submit），
 * 为 L2+ 子 agent 提供合法的任务上下文。复用通用 Modal 组件，
 * 表单字段风格跟随 SessionEditModal（原生 input/select/textarea）。
 */

import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { Loader2, Plus } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Modal } from '@/components/ui/Modal'
import { useAgentStore } from '@/stores/agentStore'
import { createRootTask, getContainerTasks } from '@/services/api/tasks'

interface CreateTaskModalProps {
  /** 是否打开 */
  isOpen: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 当前会话 ID（=thread_id），作为根任务的 session 归属 */
  sessionId: string
  /** 创建成功后回调（用于刷新任务树） */
  onCreated: () => void
}

/** 隔离模式选项 */
const ISOLATION_LEVELS = [
  { value: '', label: '默认（隔离）' },
  { value: 'isolated', label: '隔离' },
  { value: 'non_isolated', label: '非隔离' },
] as const

/** 表单字段 input 通用样式（与 SessionEditModal 一致） */
const fieldClass =
  'bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border px-3 py-1.5 text-sm outline-none transition-colors'

/**
 * 新建根任务模态框
 */
export const CreateTaskModal = memo<CreateTaskModalProps>(
  ({ isOpen, onClose, sessionId, onCreated }) => {
    const [title, setTitle] = useState('')
    const [description, setDescription] = useState('')
    const [taskScope, setTaskScope] = useState<'container' | 'non_container'>('non_container')
    const [targetId, setTargetId] = useState('')
    const [workspace, setWorkspace] = useState('')
    const [isolationLevel, setIsolationLevel] = useState('')
    const [isSubmitting, setIsSubmitting] = useState(false)
    const [parentTaskId, setParentTaskId] = useState('')
    const [containers, setContainers] = useState<Array<{ id: string; title: string }>>([])

    const agents = useAgentStore((state) => state.agents)
    const availableAgents = useMemo(
      () => agents.filter((a) => a.status === 'active'),
      [agents],
    )

    // 选了父容器 → 子任务必然 non_container，workspace 继承父容器（隐藏该字段）
    const isChildTask = parentTaskId.trim().length > 0

    // 打开时重置字段 + 拉取当前会话的容器任务（供选父容器）
    useEffect(() => {
      if (isOpen) {
        setTitle('')
        setDescription('')
        setTaskScope('non_container')
        setTargetId('')
        setWorkspace('')
        setIsolationLevel('')
        setParentTaskId('')
        if (sessionId) {
          getContainerTasks(sessionId)
            .then(setContainers)
            .catch(() => setContainers([]))
        }
      }
    }, [isOpen, sessionId])

    // 容器根任务无需 agent；非容器/子任务必须指定执行 agent
    const needsAgent = isChildTask || taskScope === 'non_container'
    const canSubmit =
      title.trim().length > 0 && (!needsAgent || targetId.trim().length > 0)

    const handleSubmit = useCallback(async () => {
      if (!canSubmit || isSubmitting || !sessionId) return
      setIsSubmitting(true)
      try {
        const effectiveScope = isChildTask ? 'non_container' : taskScope
        await createRootTask({
          title: title.trim(),
          description: description.trim(),
          task_scope: effectiveScope,
          target_id: effectiveScope === 'non_container' ? targetId.trim() : '',
          workspace: workspace.trim(),
          isolation_level: isolationLevel,
          thread_id: sessionId,
          parent_task_id: parentTaskId.trim() || undefined,
        })
        toast.success(
          isChildTask
            ? '子任务已创建，工作空间继承父容器'
            : taskScope === 'container'
              ? '工作空间已创建'
              : '任务已创建并开始执行',
        )
        onCreated()
        onClose()
      } catch (e) {
        toast.error('创建失败', {
          description: e instanceof Error ? e.message : String(e),
        })
      } finally {
        setIsSubmitting(false)
      }
    }, [
      canSubmit, isSubmitting, sessionId,
      title, description, taskScope, targetId, workspace, isolationLevel, parentTaskId,
      isChildTask,
      onCreated, onClose,
    ])

    return (
      <Modal
        open={isOpen}
        onClose={onClose}
        title="新建任务"
        maxWidth="md"
      >
        <div className="space-y-4">
          {/* 标题 */}
          <div>
            <label className="text-foreground mb-1 block text-sm font-medium">标题</label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className={fieldClass}
              placeholder="输入任务标题..."
              autoFocus
            />
          </div>

          {/* 描述 */}
          <div>
            <label className="text-foreground mb-1 block text-sm font-medium">描述（可选）</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className={`${fieldClass} min-h-[72px] resize-y`}
              placeholder="任务详细描述..."
              rows={3}
            />
          </div>

          {/* 父容器（可选）：选了则挂为该容器的子任务，workspace 自动继承 */}
          {containers.length > 0 && (
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">
                父容器（可选，选则挂为子任务）
              </label>
              <select
                value={parentTaskId}
                onChange={(e) => setParentTaskId(e.target.value)}
                className={fieldClass}
              >
                <option value="">无（创建根任务）</option>
                {containers.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* 任务类型（选了父容器则锁定为非容器子任务） */}
          <div>
            <label className="text-foreground mb-1 block text-sm font-medium">任务类型</label>
            <select
              value={isChildTask ? 'non_container' : taskScope}
              onChange={(e) => setTaskScope(e.target.value as 'container' | 'non_container')}
              className={fieldClass}
              disabled={isChildTask}
            >
              <option value="non_container">非容器（直接执行）</option>
              <option value="container">容器（工作空间集合）</option>
            </select>
          </div>

          {/* 执行 Agent（非容器：根任务非容器 或 子任务） */}
          {(isChildTask || taskScope === 'non_container') && (
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">执行 Agent</label>
              <select
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                className={fieldClass}
              >
                <option value="">请选择...</option>
                {availableAgents.map((agent) => (
                  <option key={agent.configId || agent.id} value={agent.configId || agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* 工作空间（子任务继承父容器，不显示） */}
          {isChildTask ? (
            <p className="text-muted-foreground text-xs">工作空间将自动继承所选父容器</p>
          ) : (
            <div>
              <label className="text-foreground mb-1 block text-sm font-medium">
                工作空间（可选）
              </label>
              <input
                type="text"
                value={workspace}
                onChange={(e) => setWorkspace(e.target.value)}
                className={fieldClass}
                placeholder="留空使用默认工作空间..."
              />
            </div>
          )}

          {/* 隔离模式 */}
          <div>
            <label className="text-foreground mb-1 block text-sm font-medium">隔离模式</label>
            <select
              value={isolationLevel}
              onChange={(e) => setIsolationLevel(e.target.value)}
              className={fieldClass}
            >
              {ISOLATION_LEVELS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* 底部操作按钮 */}
        <div className="mt-6 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onClose} disabled={isSubmitting}>
            取消
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={!canSubmit || isSubmitting}>
            {isSubmitting ? (
              <>
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                创建中...
              </>
            ) : (
              <>
                <Plus className="mr-1 h-3.5 w-3.5" />
                创建
              </>
            )}
          </Button>
        </div>
      </Modal>
    )
  },
)

CreateTaskModal.displayName = 'CreateTaskModal'
