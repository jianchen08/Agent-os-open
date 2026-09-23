// @feature: FP-T12 前端适配 | @ci: frontend-test
// @bug: BUG-75 | @ci: frontend-test
/**
 * 新建会话插件字段持久化契约锁（BUG-75 排查裁决：创建链无缺陷，不复发）。
 *
 * 契约（模态自述）：插件 thread_fields 表单值随线程创建写入 thread metadata
 * （键 = x_metadata_key）→ execution_context（路径 = x_execution_path）；
 * 工作空间留空时拓扑按 x_guard.on_empty 恒 plain（helper 文档化行为）。
 *
 * BUG-75 现场证据（metadata 缺 workspace 键 + workspace_mode=plain + 隔离默认
 * 在场）与本文件第二例的兜底产物逐键吻合 = 表单状态里工作空间本就为空的指纹
 * （R247 探针误填：r247-02 标题打进拓扑选择器搜索框、r247-04 创建前工作空间
 * 输入框为空），非创建链丢弃。本文件以真实声明（workspace_lifecycle /
 * isolation 两个 plugin.json，见 sessionTestUtils）走真实组件链路（RjsfForm
 * 渲染 → 真实事件交互 → handleSave 产物）锁定两端行为，防回归。
 *
 * mock 纪律：仅 mock 外部边界（agents 查询、thread schema API、本地快照存储）；
 * vi.mock 声明因提升语义留在本文件原地。
 */
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  REAL_ISOLATION_FIELDS,
  REAL_WORKSPACE_LIFECYCLE_FIELDS,
  makeAgent,
  openAntdSelectOption,
  renderCreateModal,
  submitCreate,
} from './sessionTestUtils'

const { threadSchemaMock, agentsQueryMock, snapshotMock } = vi.hoisted(() => ({
  threadSchemaMock: vi.fn(),
  agentsQueryMock: vi.fn(),
  snapshotMock: vi.fn(),
}))

vi.mock('@/services/api/session', () => ({ getThreadSchema: threadSchemaMock }))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({ useAgentsQuery: agentsQueryMock }))
vi.mock('@/services/sessionExecutionOptions', () => ({
  loadSessionExecutionOptions: snapshotMock,
}))

beforeEach(() => {
  vi.resetAllMocks()
  agentsQueryMock.mockReturnValue({ data: [makeAgent({})] })
  snapshotMock.mockReturnValue(null)
})

describe('BUG-75：新建会话 worktree 拓扑持久化', () => {
  it('填工作空间路径 + 选 worktree → 创建载荷携带 workspace + workspace_mode=worktree', async () => {
    threadSchemaMock.mockResolvedValue([...REAL_WORKSPACE_LIFECYCLE_FIELDS, ...REAL_ISOLATION_FIELDS])
    const { onSave } = renderCreateModal()

    const input = await screen.findByLabelText('工作空间')
    fireEvent.change(input, { target: { value: 'D:/myproject/container_e17cc5927dfd' } })
    fireEvent.click(await openAntdSelectOption(/工作空间拓扑/, /worktree（隔离副本/))
    fireEvent.click(await openAntdSelectOption(/隔离模式/, /^隔离（容器）$/))
    const options = await submitCreate(onSave)
    expect(options.fieldMetadata).toMatchObject({
      workspace: 'D:/myproject/container_e17cc5927dfd',
      workspace_mode: 'worktree',
    })
    expect(options.executionContext).toMatchObject({
      workspace: {
        source_path: 'D:/myproject/container_e17cc5927dfd',
        mode: 'worktree',
      },
    })
  })

  it('工作空间留空时选 worktree → 守卫兜底恒 plain 且不携带 workspace（现有约束保持）', async () => {
    threadSchemaMock.mockResolvedValue([...REAL_WORKSPACE_LIFECYCLE_FIELDS, ...REAL_ISOLATION_FIELDS])
    const { onSave } = renderCreateModal()

    fireEvent.click(await openAntdSelectOption(/工作空间拓扑/, /worktree（隔离副本/))
    const options = await submitCreate(onSave)
    expect(options.fieldMetadata).toMatchObject({ workspace_mode: 'plain' })
    expect(options.fieldMetadata.workspace).toBeUndefined()
    expect(options.executionContext).toMatchObject({ workspace: { mode: 'plain' } })
  })
})
