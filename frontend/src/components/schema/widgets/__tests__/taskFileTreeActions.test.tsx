/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * 功能测试：任务域 file_tree 绑定（注册缝注入）
 *
 * file_tree 通用件不感知任务域——任务树的启停动作、状态词表、容器判定
 * 经 registerFileTreeDomainBinding 注入。本测试锁定绑定契约与注册缝行为
 * （只 mock 外部依赖 tasks API，绑定真实注册真实执行）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const pauseTask = vi.fn()
const resumeTask = vi.fn()

vi.mock('@/services/api/tasks', () => ({
  pauseTask: (...a: unknown[]) => pauseTask(...a),
  resumeTask: (...a: unknown[]) => resumeTask(...a),
}))

// 副作用注册：模块装载即注入任务域绑定（与组合根 registerWidgets 同路径）
import '../taskFileTreeActions'
import { getFileTreeDomainBinding, registerFileTreeDomainBinding } from '../fileTreeActions'

beforeEach(() => {
  pauseTask.mockReset().mockResolvedValue(undefined)
  resumeTask.mockReset().mockResolvedValue(undefined)
})

describe('任务域 file_tree 绑定', () => {
  it('toggleEnabled：启用走 resumeTask、停用走 pauseTask（两组区分输入）', () => {
    const binding = getFileBinding()
    binding.toggleEnabled('task-1', true)
    expect(resumeTask).toHaveBeenCalledWith('task-1')
    expect(pauseTask).not.toHaveBeenCalled()

    binding.toggleEnabled('task-2', false)
    expect(pauseTask).toHaveBeenCalledWith('task-2')
    expect(resumeTask).toHaveBeenCalledTimes(1)
  })

  it('isEnabled：活跃态为真，终态/未知为假（性质断言：七态二分）', () => {
    const binding = getFileBinding()
    const active = ['running', 'pending', 'evaluating']
    const inactive = ['completed', 'failed', 'timeout', 'stopped']
    for (const s of active) expect(binding.isEnabled(s)).toBe(true)
    for (const s of inactive) expect(binding.isEnabled(s)).toBe(false)
    // 未知状态不落入活跃集合（fail-closed）
    expect(binding.isEnabled('nonexistent')).toBe(false)
    expect(binding.isEnabled(undefined)).toBe(false)
  })

  it('isContainerNode：task_scope=container 为真，其余为假', () => {
    const binding = getFileBinding()
    expect(binding.isContainerNode?.({ task_scope: 'container' })).toBe(true)
    expect(binding.isContainerNode?.({ task_scope: 'task' })).toBe(false)
    expect(binding.isContainerNode?.({})).toBe(false)
  })

  it('状态词表：normalize 折叠别名、筛选选项覆盖七态、默认筛选 running', () => {
    const { statuses } = getFileBinding()
    // 归一化幂等 + 未知值折叠到 unknown（不抛错）
    expect(statuses.normalize('running')).toBe('running')
    expect(statuses.normalize(statuses.normalize('paused-alias-or-unknown'))).toBe(
      statuses.normalize('paused-alias-or-unknown'),
    )
    // 筛选选项与配置键同源（词表全集可筛、可显示）
    const optionValues = statuses.filterOptions.map((o) => o.value)
    expect(optionValues).toHaveLength(7)
    for (const value of optionValues) {
      expect(statuses.config[value]).toBeDefined()
    }
    expect(statuses.defaultFilter).toBe('running')
  })

  it('注册缝：后注册覆盖（单绑定模型），未注册面由调用方退化处理', () => {
    const original = getFileBinding()
    expect(original?.id).toBe('tasks')

    registerFileTreeDomainBinding({
      id: 'test-domain',
      statuses: {
        config: {},
        filterOptions: [],
        normalize: (s) => s,
        active: new Set(['on']),
        defaultFilter: '',
      },
      toggleEnabled: async () => {},
      isEnabled: (s) => s === 'on',
    })
    expect(getFileBinding()?.id).toBe('test-domain')

    // 恢复任务域绑定，避免污染同文件后续用例
    registerFileTreeDomainBinding(original!)
  })
})

function getFileBinding() {
  const binding = getFileTreeDomainBinding()
  if (!binding) throw new Error('任务域绑定未注册（taskFileTreeActions 副作用未执行）')
  return binding
}
