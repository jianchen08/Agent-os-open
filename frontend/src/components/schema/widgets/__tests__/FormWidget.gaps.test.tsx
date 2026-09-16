/**
 * 交互缺口补齐测试（G1/G2）
 *
 * G1（反馈文案/成功动作）：FormWidget successText/failureText/successAction
 * 声明化 + endpoint 提交协议（无 error/reason 即成功，通用表单端点不再误判失败）
 * G2（级联选择）：datasourceUri {{字段}} 模板随表单值渲染 + dependsOn 依赖变更
 * 自动重拉（fetchDatasourceOptions 对绝对 URI 走 apiClient.get——以 apiGet 断言）
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { useSessionStore } from '@/stores/sessionStore'
import { FormWidget } from '../FormWidget'

const { useSessionsQueryMock } = vi.hoisted(() => ({ useSessionsQueryMock: vi.fn() }))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: useSessionsQueryMock,
}))

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
// readbackUri 依赖 useActivePipelineId：固定一个已绑定管道标签的 tab
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: (sel: (s: unknown) => unknown) =>
    sel({ activeTabId: 'tab-1', tabs: [{ id: 'tab-1', pipelineRunId: 'p1' }] }),
}))
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: (sel: (s: unknown) => unknown) => sel({ activePipelineId: 'p1' }),
}))

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

const textField = (name: string, label: string) =>
  ({ name, type: 'input' as const, label })

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
  useSessionsQueryMock.mockReturnValue({ data: [] })
  useSessionStore.setState({ activeSessionId: null })
})

describe('G1：反馈文案/成功动作声明化', () => {
  it('successText 覆盖提交成功文案', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FormWidget fields={[textField('title', '标题')]} onSubmit={onSubmit} successText="任务下发成功" />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(screen.getByText('任务下发成功')).toBeInTheDocument())
  })

  it('failureText 覆盖失败文案（onSubmit 抛错）', async () => {
    render(
      <FormWidget
        fields={[textField('x', 'X')]}
        onSubmit={vi.fn().mockRejectedValue(new Error('boom'))}
        failureText="自定义失败文案"
      />,
    )
    fireEvent.change(screen.getByLabelText('X'), { target: { value: 'v' } })
    submitForm()
    await waitFor(() => expect(screen.getByText('自定义失败文案')).toBeInTheDocument())
  })

  it('endpoint 响应无 error/reason → 成功（通用表单端点不再误判失败）', async () => {
    apiPost.mockResolvedValue({ data: { task_id: 't-1' } })
    render(
      <FormWidget
        fields={[textField('title', '标题')]}
        endpoint="/ext/task_service/tasks/root"
        submitLabel="创建"
        successText="已创建任务"
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() =>
      expect(screen.getByTestId('form-widget-status').textContent).toBe('已创建任务'),
    )
  })

  it('successAction.open_panel：成功后按声明路径打开面板（不抛错）', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FormWidget
        fields={[textField('title', '标题')]}
        onSubmit={onSubmit}
        successAction={{ type: 'open_panel', path: '/cost' }}
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
  })

  it('successAction.reload：成功后 datasource 重拉（再次 GET）', async () => {
    apiGet.mockImplementation((_url: string) =>
      Promise.resolve({ data: { fields: [textField('v', 'V')] } }),
    )
    apiRequest.mockResolvedValue({ data: {} })
    render(
      <FormWidget
        fieldsUri="/ext/agent_manager/agents/schema"
        dataUri="/ext/agent_manager/agents/x/config"
        dataFormat="json"
        successAction={{ type: 'reload' }}
      />,
    )
    await screen.findByLabelText('V')
    const getsBefore = apiGet.mock.calls.length
    submitForm()
    await waitFor(() => expect(apiRequest).toHaveBeenCalled())
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore))
  })
})

describe('G3：readbackUri 回读当前值（权限模式选择器）', () => {
  it('挂载时 GET 回读并刷新选择器显示；提交成功后再次回读', async () => {
    // 顺序：①挂载回读 → mode=bypass ②POST 提交成功 ③提交后回读 → mode=default
    // （显式选择过 → explicit=true，回读值才回填显示）
    apiGet
      .mockResolvedValueOnce({ data: { mode: 'bypass', explicit: true } })
      .mockResolvedValueOnce({ data: { mode: 'default', explicit: true } })
    apiPost.mockResolvedValue({ data: { switched: true } })

    render(
      <FormWidget
        fields={[
          {
            name: 'mode',
            type: 'select' as const,
            label: '权限模式',
            options: [
              { label: '默认（命中规则才确认）', value: 'default' },
              { label: '旁路（跳过审批）', value: 'bypass' },
            ],
          },
        ]}
        endpoint="/ext/pipeline_security_check/permission_mode"
        readbackUri="/ext/pipeline_security_check/permission_mode"
      />,
    )
    // 挂载回读生效：按钮显示旁路档（而非默认占位）
    await waitFor(() =>
      expect(screen.getByTestId('compact-select-trigger').textContent).toContain('旁路'),
    )
    // 点开菜单选择默认档 → POST → 提交后回读 → 按钮回到默认
    // Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click）
    const trigger = screen.getByTestId('compact-select-trigger')
    fireEvent.pointerDown(trigger)
    fireEvent.pointerUp(trigger)
    fireEvent.click(trigger)
    fireEvent.click(await screen.findByText('默认（命中规则才确认）'))
    await waitFor(() =>
      expect(screen.getByTestId('compact-select-trigger').textContent).toContain(
        '默认（命中规则才确认）',
      ),
    )
  })
})

describe('G4：紧凑选择器触发器自标识（BUG-6 回归）', () => {
  // BUG-6：思考强度触发器可访问名是裸「高」，GUI 脚本按「高」找权限档误中它，
  // 误以为已切到「高」权限模式。触发器必须自标识（设置名：当前值）。
  const thinkingField = {
    name: 'strength',
    type: 'select' as const,
    label: '思考强度',
    options: [
      { label: '关闭', value: 'off' },
      { label: '低', value: 'low' },
      { label: '中', value: 'medium' },
      { label: '高', value: 'high' },
    ],
  }

  it('受控思考强度选择器：可访问名与可见文案均为「思考强度：高」', () => {
    render(<FormWidget fields={[thinkingField]} value={{ strength: 'high' }} onChange={vi.fn()} />)
    const trigger = screen.getByRole('button', { name: '思考强度：高' })
    expect(trigger).toHaveTextContent('思考强度：高')
  })

  it('裸「高」精确名按钮不再存在（按「高」找权限档不再误中思考强度）', () => {
    render(<FormWidget fields={[thinkingField]} value={{ strength: 'high' }} onChange={vi.fn()} />)
    expect(screen.queryByRole('button', { name: '高' })).not.toBeInTheDocument()
  })

  it('权限模式选择器（endpoint 直连）：可访问名带设置标识「权限模式：…」', async () => {
    apiGet.mockResolvedValue({ data: { mode: 'default' } })
    render(
      <FormWidget
        fields={[
          {
            name: 'mode',
            type: 'select' as const,
            label: '权限模式',
            options: [
              { label: '默认（命中规则才确认）', value: 'default' },
              { label: '旁路（跳过审批）', value: 'bypass' },
            ],
          },
        ]}
        endpoint="/ext/pipeline_security_check/permission_mode"
      />,
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '权限模式：默认（命中规则才确认）' })).toBeInTheDocument(),
    )
  })
})

describe('隔离会话权限档如实显示（免审批默认 + 显式覆盖）', () => {
  // 语义（用户裁定 2026-09-15）：隔离/worktree 会话未显式选择权限档时后端
  // 默认完全免审批（explicit=false 回读），选择器必须显示该生效默认；
  // 显式选择任一档（explicit=true 回读）后显示所选档。
  const permissionField = {
    name: 'mode',
    type: 'select' as const,
    label: '权限模式',
    options: [
      { label: '默认（命中规则才确认）', value: 'default' },
      { label: '接受编辑（文件自动）', value: 'accept_edits' },
    ],
  }

  const renderSelector = () =>
    render(
      <FormWidget
        fields={[permissionField]}
        endpoint="/ext/pipeline_security_check/permission_mode"
        readbackUri="/ext/pipeline_security_check/permission_mode"
      />,
    )

  const openMenu = async (trigger: HTMLElement) => {
    fireEvent.pointerDown(trigger)
    fireEvent.pointerUp(trigger)
    fireEvent.click(trigger)
  }

  it('隔离会话未显式选择（explicit=false 回读）→ 显示「免审批（隔离默认）」', async () => {
    useSessionsQueryMock.mockReturnValue({
      data: [{ id: 's-iso', isolationMode: 'isolated', workspaceMode: 'plain' }],
    })
    useSessionStore.setState({ activeSessionId: 's-iso' })
    apiGet.mockResolvedValue({ data: { mode: 'default', explicit: false } })

    renderSelector()
    const trigger = await screen.findByTestId('compact-select-trigger')
    await waitFor(() => expect(trigger.textContent).toContain('免审批（隔离默认）'))
    expect(
      screen.getByRole('button', { name: '权限模式：免审批（隔离默认）' }),
    ).toBeInTheDocument()
    // 未显式选择 = 无档位生效勾选（免审批不是四档之一，是隔离默认态）
    await openMenu(trigger)
    const items = await screen.findAllByRole('menuitem')
    const checked = items.filter((el) => el.querySelector('svg'))
    expect(checked).toEqual([])
  })

  it('worktree 会话未显式选择 → 同样显示「免审批（隔离默认）」', async () => {
    useSessionsQueryMock.mockReturnValue({
      data: [{ id: 's-wt', isolationMode: null, workspaceMode: 'worktree' }],
    })
    useSessionStore.setState({ activeSessionId: 's-wt' })
    apiGet.mockResolvedValue({ data: { mode: 'default', explicit: false } })

    renderSelector()
    const trigger = await screen.findByTestId('compact-select-trigger')
    await waitFor(() => expect(trigger.textContent).toContain('免审批（隔离默认）'))
  })

  it('非隔离会话未显式选择 → 显示默认档（生效语义与显示一致）', async () => {
    useSessionsQueryMock.mockReturnValue({
      data: [{ id: 's-ni', isolationMode: 'non_isolated', workspaceMode: 'plain' }],
    })
    useSessionStore.setState({ activeSessionId: 's-ni' })
    apiGet.mockResolvedValue({ data: { mode: 'default', explicit: false } })

    renderSelector()
    const trigger = await screen.findByTestId('compact-select-trigger')
    await waitFor(() =>
      expect(trigger.textContent).toContain('默认（命中规则才确认）'),
    )
  })

  it('隔离会话显式选择（explicit=true 回读）→ 显示所选档而非免审批', async () => {
    useSessionsQueryMock.mockReturnValue({
      data: [{ id: 's-iso2', isolationMode: 'isolated', workspaceMode: null }],
    })
    useSessionStore.setState({ activeSessionId: 's-iso2' })
    apiGet.mockResolvedValue({ data: { mode: 'accept_edits', explicit: true } })

    renderSelector()
    const trigger = await screen.findByTestId('compact-select-trigger')
    await waitFor(() =>
      expect(trigger.textContent).toContain('接受编辑（文件自动）'),
    )
    expect(trigger.textContent).not.toContain('免审批')
  })
})

describe('G2：级联选择（datasourceUri 模板 + dependsOn 重拉）', () => {
  it('{{field}} 模板随表单值渲染并重拉（文本字段驱动，jsdom 可靠）', async () => {
    apiGet.mockResolvedValue({ data: [] })
    render(
      <RjsfForm
        fields={[
          { name: 'provider', type: 'input' as const, label: '提供商' },
          {
            name: 'model',
            type: 'select' as const,
            label: '模型',
            datasourceUri: '/ext/opts?provider={{provider}}',
          },
        ]}
      />,
    )
    // 初始空值 → 模板空段
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider='))
    // 输入 provider → onChange → formData 更新 → 模板实值重拉
    fireEvent.change(screen.getByLabelText('提供商'), { target: { value: 'zhipu' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=zhipu'))
    // 再改一次 → 依赖变化再次重拉
    fireEvent.change(screen.getByLabelText('提供商'), { target: { value: 'openai' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=openai'))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=zhipu'))
  })

  it('显式 dependsOn 字段变化触发重拉（datasourceUri 无模板时也生效）', async () => {
    apiGet.mockResolvedValue({ data: [] })
    render(
      <RjsfForm
        fields={[
          { name: 'scope', type: 'input' as const, label: '范围' },
          { name: 'item', type: 'select' as const, label: '条目', datasourceUri: '/ext/items', dependsOn: ['scope'] },
        ]}
      />,
    )
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(1))
    // 依赖字段变化 → 同一 URI 重拉（无模板，dependsOn 驱动）
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'task' } })
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(2))
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'session' } })
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(3))
    // 依赖值未变（重复输入同值）→ 不重拉
    const stable = apiGet.mock.calls.length
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'session' } })
    await new Promise((r) => setTimeout(r, 200))
    expect(apiGet.mock.calls.length).toBe(stable)
  })
})
