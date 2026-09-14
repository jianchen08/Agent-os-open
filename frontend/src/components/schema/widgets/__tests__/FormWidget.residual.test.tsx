/**
 * FormWidget 残余分支补测（簇3，与既有 FormWidget.* 五个测试文件互补）
 *
 * 覆盖既有测试未触达的分支：
 * - createSession 声明：无激活管道时渲染「新建会话」入口；有管道时不渲染
 * - 会话创建成功 → 关模态；创建失败 → toast 错误且模态保留（isCreatingSession 复位）
 * - ModalShell：受控 open 时不自渲染 trigger；非受控时 trigger 按钮开模态、
 *   「取消」按钮关闭并回调 onClose
 * - datasource 写回失败 → 状态文案回落错误信息（非 yaml 分支）
 * - DecisionFormAdapter：单选项（radio）与多选项（checkbox）的 onChange 载荷
 * - CompactSelectToggle 的 onPick 优先于 onSelect（受控模式下只回调宿主）
 *
 * mock 纪律：仅 mock 外部依赖（HTTP 客户端 / toast 通道 / 会话创建服务 /
 * store 选择器）。会话创建 mock 位于业务边界（跨 store 编排），非被测实现细节。
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from '@/components/ui/sonner'
import { renderWithProviders } from '@/test/renderWithProviders'
import { FormWidget, DecisionFormAdapter } from '../FormWidget'
import { RjsfForm } from '@/services/schema/RjsfForm'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    {
      get: (...args: unknown[]) => apiGet(...args),
      post: (...args: unknown[]) => apiPost(...args),
    },
  ),
  apiClient: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    {
      get: (...args: unknown[]) => apiGet(...args),
      post: (...args: unknown[]) => apiPost(...args),
    },
  ),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const createSessionWithProject = vi.fn()
vi.mock('@/services/sessionCreation', () => ({
  createSessionWithProject: (...args: unknown[]) => createSessionWithProject(...args),
  registerSessionProject: vi.fn(),
}))

// 无激活管道（createSession 入口的触发条件）
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: (sel: (s: unknown) => unknown) => sel({ activeTabId: null, tabs: [] }),
}))
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: (sel: (s: unknown) => unknown) => sel({ activePipelineId: null }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: unknown) => unknown) => sel({ activeSessionId: null }),
}))

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

const textField = (name: string, label: string) => ({ name, type: 'input' as const, label })

beforeEach(() => {
  vi.clearAllMocks()
  apiGet.mockResolvedValue({ data: {} })
  apiPost.mockResolvedValue({ data: {} })
  apiRequest.mockResolvedValue({ data: {} })
})

describe('createSession 声明：无激活管道时的入口', () => {
  // SessionEditModal 内部走 react-query（agents 查询），需 Provider 包裹
  const renderWithQuery = (ui: React.ReactElement) => renderWithProviders(ui)

  it('createSession=true + endpoint + 无管道 → 渲染「新建会话」提示与按钮', () => {
    renderWithQuery(
      <FormWidget
        fields={[textField('title', '标题')]}
        endpoint="/ext/tasks/root"
        createSession
      />,
    )
    expect(screen.getByText(/当前没有激活管道/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新建会话' })).toBeInTheDocument()
  })

  it('未声明 createSession → 不渲染入口（一字不增）', () => {
    renderWithQuery(<FormWidget fields={[textField('title', '标题')]} endpoint="/ext/tasks/root" />)
    expect(screen.queryByText(/当前没有激活管道/)).not.toBeInTheDocument()
  })

  it('createSession 但无 endpoint → 不渲染入口（提交必缺目标，入口无意义）', () => {
    renderWithQuery(<FormWidget fields={[textField('title', '标题')]} createSession />)
    expect(screen.queryByText(/当前没有激活管道/)).not.toBeInTheDocument()
  })

  it('点「新建会话」→ 打开会话创建模态（标题输入可见）', async () => {
    renderWithQuery(
      <FormWidget fields={[textField('title', '标题')]} endpoint="/ext/tasks/root" createSession />,
    )
    fireEvent.click(screen.getByRole('button', { name: '新建会话' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByPlaceholderText(/输入会话标题/)).toBeInTheDocument()
  })

  it('创建会话失败 → toast 错误，模态保留（用户可重试）', async () => {
    createSessionWithProject.mockRejectedValue(new Error('后端拒绝'))
    renderWithQuery(
      <FormWidget fields={[textField('title', '标题')]} endpoint="/ext/tasks/root" createSession />,
    )
    fireEvent.click(screen.getByRole('button', { name: '新建会话' }))

    const dialog = await screen.findByRole('dialog')
    const titleInput = within(dialog).getByPlaceholderText(/输入会话标题/)
    fireEvent.change(titleInput, { target: { value: '新会话' } })
    // SessionEditModal 的提交按钮（mode=create 且标题非空才可用）
    fireEvent.click(within(dialog).getByRole('button', { name: /创建|保存/ }))

    await waitFor(() => expect(createSessionWithProject).toHaveBeenCalled())
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('创建会话失败', expect.objectContaining({
        description: '后端拒绝',
      })),
    )
    // 失败后模态保留（标题输入仍可编辑，便于重试）
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('创建会话成功 → 模态关闭且会话标题输入消失', async () => {
    createSessionWithProject.mockResolvedValue(undefined)
    renderWithQuery(
      <FormWidget fields={[textField('title', '标题')]} endpoint="/ext/tasks/root" createSession />,
    )
    fireEvent.click(screen.getByRole('button', { name: '新建会话' }))

    // FormWidget 自身也有「标题」字段，须在会话模态范围内定位
    const dialog = await screen.findByRole('dialog')
    const titleInput = within(dialog).getByPlaceholderText(/输入会话标题/)
    fireEvent.change(titleInput, { target: { value: '成功会话' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /创建|保存/ }))

    await waitFor(() =>
      expect(createSessionWithProject).toHaveBeenCalledWith(
        '成功会话',
        null,
        expect.anything(),
        'FormWidget',
      ),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('模态内点「取消」→ 关闭会话模态（onClose 回调接通）', async () => {
    renderWithQuery(
      <FormWidget fields={[textField('title', '标题')]} endpoint="/ext/tasks/root" createSession />,
    )
    fireEvent.click(screen.getByRole('button', { name: '新建会话' }))
    const dialog = await screen.findByRole('dialog')

    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('modal 壳模式', () => {
  const modalFields = [textField('title', '标题')]

  it('非受控 + trigger 文本 → 渲染触发按钮，点击后模态打开', () => {
    render(<FormWidget fields={modalFields} onSubmit={vi.fn()} modal={{ trigger: '打开表单' }} />)

    const trigger = screen.getByRole('button', { name: '打开表单' })
    fireEvent.click(trigger)

    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('非受控 + 点「取消」→ 模态关闭并回调 onClose', () => {
    const onClose = vi.fn()
    render(
      <FormWidget
        fields={modalFields}
        onSubmit={vi.fn()}
        modal={{ trigger: '打开表单', title: '填写' }}
        onClose={onClose}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '打开表单' }))
    fireEvent.click(screen.getByRole('button', { name: '取消' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('受控 open → 不渲染 trigger 按钮（由宿主控制开合）', () => {
    render(
      <FormWidget
        fields={modalFields}
        onSubmit={vi.fn()}
        modal={{ trigger: '不应出现' }}
        open
        onClose={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: '不应出现' })).not.toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('受控 open=false → 模态关闭（open 直通 Dialog）', () => {
    render(
      <FormWidget fields={modalFields} onSubmit={vi.fn()} modal={{}} open={false} onClose={vi.fn()} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('受控模态经 Esc 关闭 → onClose 回调（Dialog onOpenChange 桥接）', async () => {
    const onClose = vi.fn()
    render(
      <FormWidget fields={modalFields} onSubmit={vi.fn()} modal={{ title: '受控' }} open onClose={onClose} />,
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    fireEvent.keyDown(document.body, { key: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('modal 模式不启用紧凑下拉（即使字段是单 select + endpoint）', () => {
    render(
      <FormWidget
        fields={[
          {
            name: 'mode',
            type: 'select' as const,
            label: '模式',
            options: [{ label: 'A', value: 'a' }],
          },
        ]}
        endpoint="/ext/x"
        modal={{ trigger: '打开' }}
      />,
    )
    expect(screen.queryByTestId('compact-select-trigger')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '打开' })).toBeInTheDocument()
  })
})

describe('datasource/json 写回失败', () => {
  it('提交失败（Error）→ 状态文案取错误信息，错误事件不被吞', async () => {
    apiGet.mockImplementation((url: string) =>
      url === '/ext/f'
        ? Promise.resolve({ data: [{ name: 'v', type: 'input', label: 'V' }] })
        : Promise.resolve({ data: { v: 'x' } }),
    )
    apiRequest.mockRejectedValue(new Error('磁盘只读'))

    render(<FormWidget fieldsUri="/ext/f" dataUri="/ext/d" dataFormat="json" />)
    await screen.findByLabelText('V')
    submitForm()

    expect(await screen.findByTestId('form-widget-status')).toHaveTextContent('磁盘只读')
  })

  it('提交失败（非 Error 拒因）→ 回落「保存失败」兜底文案', async () => {
    apiGet.mockImplementation((url: string) =>
      url === '/ext/f'
        ? Promise.resolve({ data: [{ name: 'v', type: 'input', label: 'V' }] })
        : Promise.resolve({ data: { v: 'x' } }),
    )
    apiRequest.mockRejectedValue({ code: 'E_IO' })

    render(<FormWidget fieldsUri="/ext/f" dataUri="/ext/d" dataFormat="json" />)
    await screen.findByLabelText('V')
    submitForm()

    expect(await screen.findByTestId('form-widget-status')).toHaveTextContent('保存失败')
  })

  it('dataUri 返回数组（非对象）→ 初值为空对象，表单仍可渲染', async () => {
    apiGet.mockImplementation((url: string) =>
      url === '/ext/f'
        ? Promise.resolve({ data: { fields: [{ name: 'v', type: 'input', label: 'V' }] } })
        : Promise.resolve({ data: [1, 2, 3] }),
    )

    render(<FormWidget fieldsUri="/ext/f" dataUri="/ext/d" dataFormat="json" />)
    expect(await screen.findByLabelText('V')).toHaveValue('')
  })

  it('fieldsUri 响应既非数组也非 {fields} → 出错态文案可见', async () => {
    apiGet.mockImplementation((url: string) =>
      url === '/ext/f' ? Promise.resolve({ data: { nope: true } }) : Promise.resolve({ data: {} }),
    )

    render(<FormWidget fieldsUri="/ext/f" />)
    expect(await screen.findByRole('alert')).toHaveTextContent('fieldsUri 响应不含 fields 数组')
  })
})

describe('DecisionFormAdapter', () => {
  const options = [
    { id: 'a', label: '方案甲', description: '快速' },
    { id: 'b', label: '方案乙' },
  ]

  it('单选项（radio）→ 点选回传字符串 id，选项描述并入标签', () => {
    const onDecision = vi.fn()
    render(<DecisionFormAdapter options={options} onDecision={onDecision} title="选方案" />)

    expect(screen.getByText('方案甲（快速）')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('方案甲（快速）'))
    expect(onDecision).toHaveBeenCalledWith('a')
  })

  it('多选项（checkbox）→ 回传 id 数组', () => {
    const onDecision = vi.fn()
    render(<DecisionFormAdapter options={options} multiple onDecision={onDecision} />)

    fireEvent.click(screen.getByLabelText('方案乙'))
    const [arg] = onDecision.mock.calls[onDecision.mock.calls.length - 1]
    expect(Array.isArray(arg)).toBe(true)
    expect(arg).toContain('b')
  })

  it('无选项 → 渲染空表单占位（不崩）', () => {
    const { container } = render(<DecisionFormAdapter options={[]} />)
    expect(container.querySelector('form') ?? container.firstChild).toBeTruthy()
  })

  it('非法 options（非数组/缺 id）→ 视为无选项', () => {
    render(<DecisionFormAdapter options={'oops' as unknown as unknown[]} />)
    render(<DecisionFormAdapter options={[{ label: '缺 id' }] as unknown as unknown[]} />)
    expect(screen.queryByLabelText('缺 id')).not.toBeInTheDocument()
  })

  it('未声明 onDecision → 点选不抛错（只读展示）', () => {
    render(<DecisionFormAdapter options={options} />)
    expect(() => fireEvent.click(screen.getByLabelText('方案乙'))).not.toThrow()
  })
})

describe('CompactSelectToggle 受控模式', () => {
  it('声明 onChange → 点选只回调宿主（onPick 优先，不触发提交）', async () => {
    const onChange = vi.fn()
    apiPost.mockClear()
    render(
      <FormWidget
        fields={[
          {
            name: 'mode',
            type: 'select' as const,
            label: '模式',
            options: [
              { label: '甲', value: 'a' },
              { label: '乙', value: 'b' },
            ],
          },
        ]}
        endpoint="/ext/mode"
        onChange={onChange}
      />,
    )

    const trigger = screen.getByTestId('compact-select-trigger')
    fireEvent.pointerDown(trigger)
    fireEvent.pointerUp(trigger)
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByText('乙'))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ mode: 'b' }))
    // 受控模式不直连端点：onPick 命中即返回
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('受控初值显示宿主值对应的选项文案', () => {
    render(
      <FormWidget
        value={{ mode: 'b' }}
        onChange={vi.fn()}
        fields={[
          {
            name: 'mode',
            type: 'select' as const,
            label: '模式',
            options: [
              { label: '甲', value: 'a' },
              { label: '乙', value: 'b' },
            ],
          },
        ]}
      />,
    )
    expect(screen.getByTestId('compact-select-trigger')).toHaveTextContent('乙')
  })

  it('disabled 声明 → 触发器禁用不可点', () => {
    render(
      <FormWidget
        fields={[
          { name: 'mode', type: 'select' as const, label: '模式', options: [{ label: '甲', value: 'a' }] },
        ]}
        onChange={vi.fn()}
        disabled
      />,
    )
    expect(screen.getByTestId('compact-select-trigger')).toBeDisabled()
  })
})

describe('RjsfForm 空字段形态', () => {
  it('fields=[] → 渲染表单容器但不崩（决策适配器的空选项路径）', () => {
    const { container } = render(<RjsfForm fields={[]} />)
    expect(container.firstChild).toBeTruthy()
  })
})
