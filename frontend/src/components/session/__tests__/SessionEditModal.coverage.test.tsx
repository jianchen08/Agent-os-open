// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * SessionEditModal 交互面补测（声明翻译层见 SessionEditModal.declaration.test.ts）
 *
 * 覆盖组件可观察行为：
 * - 关闭触发（Radix onOpenChange(false)，含内置 Close 按钮）→ 回调 onClose
 * - 插件字段 schema 拉取失败 → 降级为空表单（不阻断标题/Agent 编辑）
 * - Agent 下拉：只列 active Agent；选中值以 configId 优先作为提交 agentId
 * - create 模式：标题可空（提交时兜底「新会话」）；edit 模式标题为空时禁用保存
 * - 插件字段表单变更经 onChange 回写，保存时并入 options（存储键 + 执行路径）
 *
 * mock 纪律：仅 mock 外部边界（agents 查询、thread schema API、本地快照存储）。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { SessionEditModal } from '../SessionEditModal'
import { makeAgent, submitCreate } from './sessionTestUtils'
import type { Session } from '@/types'

const getThreadSchemaMock = vi.hoisted(() => vi.fn())
const useAgentsQueryMock = vi.hoisted(() => vi.fn())
const loadSnapshotMock = vi.hoisted(() => vi.fn())

vi.mock('@/services/api/session', () => ({
  getThreadSchema: getThreadSchemaMock,
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: useAgentsQueryMock,
}))
vi.mock('@/services/sessionExecutionOptions', () => ({
  loadSessionExecutionOptions: loadSnapshotMock,
}))

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: 'sess-1',
    title: '既有会话',
    agentId: 'agentos',
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    ...overrides,
  } as Session
}

const workspaceField = {
  name: 'workspace',
  type: 'string',
  label: '工作空间',
  x_metadata_key: 'workspace',
  x_execution_path: 'workspace.source_path',
}

function renderModal(props: Partial<React.ComponentProps<typeof SessionEditModal>> = {}) {
  const onClose = vi.fn()
  const onSave = vi.fn()
  const view = renderWithProviders(
    <SessionEditModal
      mode="create"
      isOpen
      session={null}
      onClose={onClose}
      onSave={onSave}
      {...props}
    />,
  )
  return { ...view, onClose, onSave }
}

beforeEach(() => {
  vi.resetAllMocks()
  useAgentsQueryMock.mockReturnValue({ data: [makeAgent({})] })
  getThreadSchemaMock.mockResolvedValue([])
  loadSnapshotMock.mockReturnValue(null)
})

describe('SessionEditModal — 关闭触发', () => {
  it('点内置关闭按钮（X）→ onClose 被调用', async () => {
    const { onClose } = renderModal()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('点「取消」→ onClose 被调用（与 X 同语义）', async () => {
    const { onClose } = renderModal()

    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('isSaving 时「取消」禁用（保存中不允许关窗丢失状态）', () => {
    renderModal({ isSaving: true })

    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  })
})

describe('SessionEditModal — 插件字段 schema 拉取', () => {
  it('拉取失败 → 降级为空表单（标题/Agent 仍可编辑，不阻断）', async () => {
    getThreadSchemaMock.mockRejectedValue(new Error('schema 端点 500'))
    renderModal()

    // 标题输入与 Agent 下拉仍在（降级只影响插件字段区）
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByPlaceholderText(/输入会话标题/)).toBeInTheDocument()
    expect(within(dialog).getByRole('combobox')).toBeInTheDocument()
    // 插件字段无声明 → 无额外表单（无「暂无表单字段」占位，因为整块不渲染）
    await waitFor(() =>
      expect(within(dialog).queryByText('暂无表单字段')).not.toBeInTheDocument(),
    )
  })

  it.each([
    { fields: [workspaceField], expectedLabel: '工作空间' },
    { fields: [], expectedLabel: null },
  ])(
    'schema 返回 $fields.length 个字段 → 插件表单渲染 $expectedLabel',
    async ({ fields, expectedLabel }) => {
      getThreadSchemaMock.mockResolvedValue(fields)
      renderModal()

      if (expectedLabel) {
        expect(await screen.findByText(expectedLabel)).toBeInTheDocument()
      } else {
        const dialog = await screen.findByRole('dialog')
        await waitFor(() => expect(getThreadSchemaMock).toHaveBeenCalled())
        expect(within(dialog).queryByText('工作空间')).not.toBeInTheDocument()
      }
    },
  )

  it('内置字段（title/intent）被过滤：不重复渲染为插件字段', async () => {
    getThreadSchemaMock.mockResolvedValue([
      { name: 'title', type: 'string', label: '标题（内置）' },
      workspaceField,
    ])
    renderModal()

    expect(await screen.findByText('工作空间')).toBeInTheDocument()
    // 内置标题只由组件原生渲染（label「标题」一份），插件声明的同名 label 不出现
    expect(screen.queryByText('标题（内置）')).not.toBeInTheDocument()
  })
})

describe('SessionEditModal — Agent 下拉', () => {
  it.each([
    {
      agents: [makeAgent({ status: 'active', name: '活跃甲' }), makeAgent({ id: 'b-2', configId: 'b', name: '停用乙', status: 'inactive' })],
      present: ['活跃甲'],
      absent: ['停用乙'],
    },
    {
      agents: [makeAgent({ status: 'error', name: '错误丙' })],
      present: [],
      absent: ['错误丙'],
    },
  ])('只列 active Agent：$present 在场 / $absent 缺席', async ({ agents, present, absent }) => {
    useAgentsQueryMock.mockReturnValue({ data: agents })
    renderModal()

    const combo = await screen.findByRole('combobox')
    for (const name of present) {
      expect(within(combo).getByRole('option', { name })).toBeInTheDocument()
    }
    for (const name of absent) {
      expect(within(combo).queryByRole('option', { name })).not.toBeInTheDocument()
    }
  })

  it('选中 Agent 后保存 → agentId 取 configId 优先（无 configId 才用 id）', async () => {
    useAgentsQueryMock.mockReturnValue({
      data: [
        makeAgent({ id: 'a-1', configId: 'cfg-main', name: '主控' }),
        makeAgent({ id: 'raw-id-only', configId: undefined, name: '无配置ID' }),
      ],
    })
    const { onSave } = renderModal()

    const combo = await screen.findByRole('combobox')
    fireEvent.change(combo, { target: { value: 'raw-id-only' } })
    fireEvent.click(screen.getByRole('button', { name: /创建/ }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    // 无 configId → 回退 id
    expect(onSave.mock.calls[0][2]).toBe('raw-id-only')

    // configId 优先路径
    fireEvent.change(combo, { target: { value: 'cfg-main' } })
    fireEvent.click(screen.getByRole('button', { name: /创建/ }))
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2))
    expect(onSave.mock.calls[1][2]).toBe('cfg-main')
  })
})

describe('SessionEditModal — 保存门控', () => {
  it('create 模式标题可空 → 提交时标题兜底「新会话」', async () => {
    const { onSave } = renderModal()
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /创建/ }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave.mock.calls[0][0]).toBeNull()
    expect(onSave.mock.calls[0][1]).toBe('新会话')
  })

  it('edit 模式标题为空 → 保存按钮禁用（不能把已存在会话改成空标题）', async () => {
    renderModal({ mode: 'edit', session: session({ title: '' }) })
    await screen.findByRole('dialog')

    expect(screen.getByRole('button', { name: /保存/ })).toBeDisabled()
  })

  it('edit 模式填标题 → sessionId 原样回传', async () => {
    const { onSave } = renderModal({ mode: 'edit', session: session({ title: '旧标题' }) })
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave.mock.calls[0][0]).toBe('sess-1')
    expect(onSave.mock.calls[0][1]).toBe('旧标题')
  })
})

describe('SessionEditModal — 插件字段值并入保存产物', () => {
  it('无快照时初值落插件选项默认，保存并入 fieldMetadata + executionContext', async () => {
    getThreadSchemaMock.mockResolvedValue([
      {
        name: 'isolationMode',
        type: 'select',
        label: '隔离模式',
        x_metadata_key: 'isolation_mode',
        x_execution_path: 'isolation.level',
        options: [
          { label: '非隔离', value: 'non_isolated' },
          { label: '隔离', value: 'isolated' },
        ],
      },
    ])
    const { onSave } = renderModal()

    // antd Select 不接受 fireEvent.change（受控 value 由内部状态驱动），
    // 须经鼠标展开下拉再点选项，与 RjsfForm 既有用例同法。Dialog 内容
    // portal 到 body，故按 combobox 回溯容器而非用 render 的 container。
    const combo = await screen.findByRole('combobox', { name: /隔离模式/ })
    fireEvent.mouseDown(combo.closest('.ant-select')!)
    fireEvent.click(await screen.findByText('隔离'))
    const options = await submitCreate(onSave)
    expect(options.fieldMetadata).toMatchObject({ isolation_mode: 'isolated' })
    expect(options.executionContext).toEqual({ isolation: { level: 'isolated' } })
  })

  it('edit 模式本地快照优先于 thread metadata 出生值', async () => {
    getThreadSchemaMock.mockResolvedValue([
      { ...workspaceField, x_execution_path: undefined },
    ])
    loadSnapshotMock.mockReturnValue({ values: { workspace: 'D:/来自快照' } })
    const { onSave } = renderModal({
      mode: 'edit',
      session: session({ metadata: { workspace: 'D:/出生值' } as never }),
    })

    const input = await screen.findByLabelText('工作空间')
    expect((input as HTMLInputElement).value).toBe('D:/来自快照')

    fireEvent.change(input, { target: { value: 'D:/用户改过' } })
    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    await waitFor(() => expect(onSave).toHaveBeenCalled())
    expect(onSave.mock.calls[0][3].fieldMetadata).toMatchObject({ workspace: 'D:/用户改过' })
  })
})
