// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * FetchModelsModal 覆盖缺口补测
 *
 * 契约（对用户可观察的行为）：
 * - 打开时按 providerId 拉远端模型：成功 → 搜索框占位显示模型数、列表可勾选；
 * - 拉取失败 → 错误提示区显示后端 message（无 message 时用兜底文案），
 *   且提示「仍可在下方手动输入模型名添加」；
 * - 搜索框过滤：命中保留、不命中显示空态文案（两组有区分度输入）；
 * - 自定义输入回车/点「加入」加入待添加清单；重复项/已在勾选中不重复加入；
 *   空输入不加入；
 * - 待添加 chip 可移除（点 X → chip 消失、待添加计数回落）；
 * - 底部「取消」与对话框关闭（Esc / 关闭按钮）都回调 onClose；
 * - 「添加所选」把 勾选 + 自定义 一并交给 onAdd（limits 含远端元数据），成功后 onClose。
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { FetchModelsModal } from '../FetchModelsModal'

const mock = {
  getRemoteModels: vi.fn(),
}

vi.mock('@/services/api/config', () => ({
  getRemoteModels: (...a: unknown[]) => mock.getRemoteModels(...a),
}))

const remote = [
  { id: 'gpt-4o', owned_by: 'openai' },
  { id: 'gpt-4o-mini', owned_by: 'openai' },
  { id: 'o3-deep-research', owned_by: 'openai' },
]

function setup(overrides: Partial<React.ComponentProps<typeof FetchModelsModal>> = {}) {
  const onClose = vi.fn()
  const onAdd = vi.fn().mockResolvedValue(undefined)
  const utils = renderWithProviders(
    <FetchModelsModal
      open
      providerId="openai"
      onClose={onClose}
      onAdd={onAdd}
      {...overrides}
    />,
  )
  return { onClose, onAdd, ...utils }
}

beforeEach(() => {
  vi.resetAllMocks()
  mock.getRemoteModels.mockResolvedValue({ provider: 'openai', models: remote })
})

describe('FetchModelsModal 远端拉取', () => {
  it('拉取成功 → 占位符带模型总数，且 owned_by 元信息可见', async () => {
    setup()

    const search = await screen.findByPlaceholderText(/搜索 3 个模型/)
    expect(search).toBeInTheDocument()
    expect(mock.getRemoteModels).toHaveBeenCalledWith('openai')
    // 远端元信息落列表（owned_by 徽标渲染）
    expect(await screen.findByText('o3-deep-research')).toBeInTheDocument()
    expect(screen.getAllByText('openai').length).toBeGreaterThanOrEqual(3)
  })

  it('拉取失败（带 message）→ 错误区显示后端消息 + 手动输入兜底提示', async () => {
    mock.getRemoteModels.mockRejectedValue({ message: 'Key 无效' })
    setup()

    expect(await screen.findByText('Key 无效')).toBeInTheDocument()
    expect(screen.getByText('仍可在下方手动输入模型名添加。')).toBeInTheDocument()
    // 失败时列表区不渲染（loading/error 与列表互斥）
    expect(screen.queryByPlaceholderText(/搜索/)).not.toBeInTheDocument()
  })

  it('拉取失败（无 message）→ 用兜底文案「拉取模型列表失败」', async () => {
    mock.getRemoteModels.mockRejectedValue({ status: 500 })
    setup()

    expect(await screen.findByText('拉取模型列表失败')).toBeInTheDocument()
  })

  it('搜索过滤：命中子串保留，不命中显示空态', async () => {
    setup()
    const search = await screen.findByPlaceholderText(/搜索 3 个模型/)

    fireEvent.change(search, { target: { value: 'mini' } })
    expect(screen.getByText('gpt-4o-mini')).toBeInTheDocument()
    expect(screen.queryByText('o3-deep-research')).not.toBeInTheDocument()

    fireEvent.change(search, { target: { value: '不存在的模型' } })
    expect(screen.getByText('没有匹配的模型，可在下方手动输入')).toBeInTheDocument()
    expect(screen.queryByText('gpt-4o-mini')).not.toBeInTheDocument()
  })
})

describe('FetchModelsModal 待添加清单', () => {
  it('回车与「加入」两条入口都生效；空输入与重复项不重复加入', async () => {
    setup()
    const custom = await screen.findByPlaceholderText(/自定义模型名/)
    const addBtn = screen.getByRole('button', { name: '加入' })

    // 空输入：点加入无效果
    fireEvent.click(addBtn)
    expect(screen.getByText('待添加 0 个；添加后可在「模型」页展开参数设置上下文/think')).toBeInTheDocument()

    fireEvent.change(custom, { target: { value: 'my-model' } })
    fireEvent.keyDown(custom, { key: 'Enter' })
    expect(screen.getByText('my-model')).toBeInTheDocument()
    expect(screen.getByText(/待添加 1 个/)).toBeInTheDocument()

    // 重复同名 → 不重复加入
    fireEvent.change(custom, { target: { value: 'my-model' } })
    fireEvent.click(addBtn)
    expect(screen.getAllByText('my-model')).toHaveLength(1)
    expect(screen.getByText(/待添加 1 个/)).toBeInTheDocument()

    // 已在勾选列表中的 id → 也不加入自定义
    fireEvent.click(screen.getByText('gpt-4o-mini'))
    fireEvent.change(custom, { target: { value: 'gpt-4o-mini' } })
    fireEvent.keyDown(custom, { key: 'Enter' })
    expect(screen.getAllByText('gpt-4o-mini')).toHaveLength(1)
    expect(screen.getByText(/待添加 2 个/)).toBeInTheDocument()
  })

  it('chip 的移除按钮把自定义项从待添加清单摘除，计数回落', async () => {
    setup()
    const custom = await screen.findByPlaceholderText(/自定义模型名/)
    fireEvent.change(custom, { target: { value: 'to-remove' } })
    fireEvent.keyDown(custom, { key: 'Enter' })
    expect(screen.getByText(/待添加 1 个/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '移除 to-remove' }))

    expect(screen.queryByText('to-remove')).not.toBeInTheDocument()
    expect(screen.getByText(/待添加 0 个/)).toBeInTheDocument()
  })

  it('添加所选把勾选与自定义一并交给 onAdd（limits 携带远端元数据），成功后 onClose', async () => {
    const { onAdd, onClose } = setup()
    await screen.findByPlaceholderText(/搜索 3 个模型/)

    // 勾选两个远端模型
    fireEvent.click(screen.getByText('gpt-4o'))
    fireEvent.click(screen.getByText('o3-deep-research'))
    // 自定义一个
    const custom = screen.getByPlaceholderText(/自定义模型名/)
    fireEvent.change(custom, { target: { value: 'local-ft' } })
    fireEvent.keyDown(custom, { key: 'Enter' })

    fireEvent.click(screen.getByRole('button', { name: /添加所选 \(3\)/ }))

    await waitFor(() => expect(onAdd).toHaveBeenCalledTimes(1))
    const [providerId, names, limits] = onAdd.mock.calls[0]
    expect(providerId).toBe('openai')
    expect(new Set(names)).toEqual(new Set(['gpt-4o', 'o3-deep-research', 'local-ft']))
    expect(limits.get('o3-deep-research')).toEqual({ id: 'o3-deep-research', owned_by: 'openai' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('无待添加项时「添加所选」禁用，点击不触发 onAdd', async () => {
    const { onAdd } = setup()
    await screen.findByPlaceholderText(/搜索 3 个模型/)

    const btn = screen.getByRole('button', { name: /添加所选 \(0\)/ })
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(onAdd).not.toHaveBeenCalled()
  })
})

describe('FetchModelsModal 关闭路径', () => {
  it('「取消」按钮与 Esc 关闭都回调 onClose', async () => {
    const { onClose } = setup()
    await screen.findByPlaceholderText(/搜索 3 个模型/)

    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(onClose).toHaveBeenCalledTimes(1)

    // Radix 对话框 Esc 关闭 → Dialog.onOpenChange(false) → onClose
    fireEvent.keyDown(document.body, { key: 'Escape', code: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2))
  })

  it('关闭按钮（DialogHeader 内 Close）也走 onClose 回调', async () => {
    const { onClose } = setup()
    await screen.findByPlaceholderText(/搜索 3 个模型/)

    const dialog = screen.getByRole('dialog')
    const closeBtn = within(dialog).getByRole('button', { name: 'Close' })
    fireEvent.click(closeBtn)

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })
})
