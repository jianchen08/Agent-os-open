// @feature: FP-T12 前端适配 | @ci: frontend-test
// @bug: BUG-75 worktree 选项置灰 | @ci: frontend-test
/**
 * 新建会话 worktree 拓扑选项置灰测试（BUG-75 附带建议）。
 *
 * 契约：工作空间输入为空时，拓扑选择器的 worktree 选项置灰（disabled，悬浮
 * title 说明文案）；填写工作空间后恢复可选；再次清空重新置灰。x_guard 兜底
 * plain（SessionEditModal.applyGuards）保持不回退——置灰是渲染层的歧义消除，
 * 值守卫是保存层的兜底，两层同源于同一声明。
 *
 * 用真实声明（照抄 workspace_lifecycle plugin.json，见 sessionTestUtils）走
 * 真实组件链路（RjsfForm → @rjsf/antd SelectWidget，enumDisabled 语义置灰）。
 *
 * mock 纪律：仅 mock 外部边界（agents 查询、thread schema API、本地快照存储）；
 * vi.mock 声明因提升语义留在本文件原地。
 */
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  REAL_WORKSPACE_LIFECYCLE_FIELDS,
  makeAgent,
  openAntdSelectOption,
  renderCreateModal,
} from './sessionTestUtils'

const stubs = vi.hoisted(() => ({
  threadSchema: vi.fn(),
  agentsQuery: vi.fn(),
  executionOptions: vi.fn(),
}))

vi.mock('@/services/api/session', () => ({ getThreadSchema: stubs.threadSchema }))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({ useAgentsQuery: stubs.agentsQuery }))
vi.mock('@/services/sessionExecutionOptions', () => ({
  loadSessionExecutionOptions: stubs.executionOptions,
}))

beforeEach(() => {
  vi.resetAllMocks()
  stubs.agentsQuery.mockReturnValue({ data: [makeAgent({})] })
  stubs.executionOptions.mockReturnValue(null)
  stubs.threadSchema.mockResolvedValue(REAL_WORKSPACE_LIFECYCLE_FIELDS)
})

describe('BUG-75：工作空间为空时 worktree 选项置灰', () => {
  it('工作空间为空：worktree 选项 disabled 且悬浮 title 含「需先填写工作空间」', async () => {
    renderCreateModal()
    const option = await openAntdSelectOption(/工作空间拓扑/, /worktree（隔离副本/)

    expect(option).toHaveClass('ant-select-item-option-disabled')
    expect(option).toHaveAttribute('aria-disabled', 'true')
    expect(option.getAttribute('title') ?? '').toContain('需先填写工作空间')
  })

  it('填写工作空间后恢复可选（disabled 移除）', async () => {
    renderCreateModal()
    const input = await screen.findByLabelText('工作空间')
    fireEvent.change(input, { target: { value: 'D:/myproject/container_e17cc5927dfd' } })

    const option = await openAntdSelectOption(/工作空间拓扑/, /worktree（隔离副本/)
    expect(option).not.toHaveClass('ant-select-item-option-disabled')
    expect(option.getAttribute('aria-disabled')).toBe('false')
  })

  it('置灰的 worktree 选项点击不生效（选中值不被改写）', async () => {
    const { onSave } = renderCreateModal()
    const option = await openAntdSelectOption(/工作空间拓扑/, /worktree（隔离副本/)
    fireEvent.click(option)
    fireEvent.click(screen.getByRole('button', { name: /创建/ }))

    // 值守卫兜底 plain 不回退；置灰后点击也无法选中 worktree
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave.mock.calls[0][3].fieldMetadata).toMatchObject({ workspace_mode: 'plain' })
  })
})
