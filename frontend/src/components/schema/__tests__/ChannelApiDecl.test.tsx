/**
 * triggers 触发器声明契约测试（B1：TriggersPage 声明化替代）
 *
 * 读 trigger_setup_tool plugin.json（生产声明，channel_api 退役后随域迁入）
 * → 装载 → 渲染 widget_stage space=triggers：断言 table 行操作
 * （trigger/enable/disable when 分支/delete）+ 创建 form 的字段/提交，
 * 全部走声明链路。
 *
 * 契约与后端锁步（http_api.py）：行字段 trigger_id/status（非 id/enabled）；
 * 创建表单字段 = trigger_setup 工具入参（trigger_type/message/pipeline_id…），
 * pipeline_id 选项拉 /ext/trigger_setup_tool/pipelines。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import fs from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { WidgetStage } from '@/components/schema/widgets/WidgetStage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionListStore } from '@/stores/sessionListStore'

const apiGet = vi.fn()
const apiCall = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiCall(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))
// 新建会话入口（createSession 声明）打开的真实模态框依赖 react-query 等
// 宿主设施——桩化并暴露保存按钮驱动 onSave 回调
vi.mock('@/components/session/SessionEditModal', () => ({
  SessionEditModal: (props: { isOpen: boolean; onSave: (...args: unknown[]) => void }) =>
    props.isOpen ? (
      <button
        type="button"
        data-testid="mock-session-modal-save"
        onClick={() => props.onSave(null, '触发目标会话', null, { fieldMetadata: {} })}
      >
        mock保存
      </button>
    ) : null,
}))

const PLUGIN = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../../../plugins/shared/tools/triggers_ext/plugin.json'),
    'utf-8',
  ),
) as {
  id: string
  name: string
  ui_schema?: { widgets: Array<Record<string, unknown>> }
  contributes?: { pages: Array<Record<string, unknown>> }
}

beforeEach(() => {
  vi.clearAllMocks()
  apiGet.mockReset()
  apiCall.mockReset()
  apiGet.mockResolvedValue({ data: {} })
  apiCall.mockResolvedValue({ data: { ok: true } })
  usePipelineMessageStore.setState({ activePipelineId: null })
  initializeWidgets()
  contributionRegistry.loadFromSchema({
    agents: [{ id: PLUGIN.id, ui_schema: PLUGIN.ui_schema }],
    plugin_configs: [],
    plugin_contributes: [
      { plugin_id: PLUGIN.id, plugin_name: PLUGIN.name, contributes: PLUGIN.contributes },
    ],
  })
})

describe('trigger_setup_tool 触发器声明（B1）', () => {
  it('声明存在：triggers_table（列+rowActions+watch）与 triggers_create（form 契约字段）', () => {
    const widgets = contributionRegistry.getAllWidgets()
    const table = widgets.find((w) => w.id === 'triggers_table')
    expect(table?.type).toBe('table')
    const tableProps = table?.props as {
      columns?: Array<{ key: string }>
      rowActions?: Array<{ key: string; url: string; when?: { key: string; equals?: unknown } }>
      watch?: Array<{ event: string; action: string }>
    } | undefined
    // 行操作 URL 模板用真实行字段 trigger_id（_serialize 键）；启停按 status 分支
    expect(tableProps?.rowActions).toHaveLength(4)
    for (const action of tableProps?.rowActions ?? []) {
      expect(action.url).toContain('{trigger_id}')
    }
    const enable = tableProps?.rowActions?.find((a) => a.key === 'enable')
    expect(enable?.when).toEqual({ key: 'status', equals: 'pending' })
    const disable = tableProps?.rowActions?.find((a) => a.key === 'disable')
    expect(disable?.when).toEqual({ key: 'status', equals: 'active' })
    // 创建成功 → 表格重拉（G3 声明联动）
    expect(tableProps?.watch).toEqual([
      { event: 'trigger_setup_tool.created', action: 'reload' },
    ])
    // 声明列优先（curated 视图，键与 _serialize 输出对齐）
    expect(tableProps?.columns?.map((c) => c.key)).toEqual(
      expect.arrayContaining(['name', 'trigger_type', 'status', 'pipeline_id']),
    )

    const form = widgets.find((w) => w.id === 'triggers_create')
    expect(form?.type).toBe('form')
    const formProps = form?.props as {
      eventName?: string
      createSession?: boolean
      fields?: Array<{
        name: string
        datasourceUri?: string
        required?: boolean
        requiredWhen?: { field: string; equals: string }
      }>
      endpoint?: string
    } | undefined
    expect(formProps?.endpoint).toBe('/ext/trigger_setup_tool/triggers')
    expect(formProps?.eventName).toBe('trigger_setup_tool.created')
    // 无激活管道时提供「新建会话」入口（模态框复用会话创建流）
    expect(formProps?.createSession).toBe(true)
    const fields = formProps?.fields ?? []
    // 创建表单字段 = trigger_setup 工具入参（后端契约），管道选项走 /pipelines
    expect(fields.find((f) => f.name === 'trigger_type')?.required).toBe(true)
    expect(fields.find((f) => f.name === 'message')?.required).toBe(true)
    expect(fields.find((f) => f.name === 'pipeline_id')?.datasourceUri).toBe(
      '/ext/trigger_setup_tool/pipelines',
    )
    // 类型专属参数条件必填（requiredWhen 与工具入参语义锁步：X 类型必填 x 参数）
    const conditional: Record<string, { field: string; equals: string }> = {}
    for (const f of fields) {
      if (f.requiredWhen) conditional[f.name] = f.requiredWhen
    }
    expect(conditional).toEqual({
      delay_seconds: { field: 'trigger_type', equals: 'delay' },
      schedule_time: { field: 'trigger_type', equals: 'schedule' },
      interval: { field: 'trigger_type', equals: 'interval' },
      event_type: { field: 'trigger_type', equals: 'event' },
      condition: { field: 'trigger_type', equals: 'condition' },
    })

    const page = contributionRegistry.getPages().find((p) => p.id === 'triggers')
    expect(page?.path).toBe('/triggers')
    expect(page?.widget).toBe('widget_stage')
  })

  it('列表渲染 + status 分支：启用按钮只在 pending 行出现、禁用只在 active 行', async () => {
    apiGet.mockResolvedValue({
      data: {
        rows: [
          { trigger_id: 't1', name: '定时器', status: 'active' },
          { trigger_id: 't2', name: '手动器', status: 'pending' },
        ],
      },
    })
    render(<WidgetStage space="triggers" />)
    await waitFor(() => expect(screen.getByText('定时器')).toBeInTheDocument())
    const disableBtns = screen.getAllByRole('button', { name: '禁用' })
    expect(disableBtns).toHaveLength(1)
    const enableBtns = screen.getAllByRole('button', { name: '启用' })
    expect(enableBtns).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: '触发' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: '删除' })).toHaveLength(2)
  })

  it('点击「触发」→ POST {trigger_id} 模板端点 → 成功后重拉', async () => {
    apiGet.mockResolvedValue({
      data: { rows: [{ trigger_id: 't9', status: 'pending', name: 'n' }] },
    })
    render(<WidgetStage space="triggers" />)
    await waitFor(() => expect(screen.getAllByRole('button', { name: '触发' }).length).toBe(1))
    apiCall.mockClear()
    const getsBefore = apiGet.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '触发' }))
    await waitFor(() => expect(apiCall).toHaveBeenCalled())
    expect(apiCall.mock.calls[0][0]).toMatchObject({
      method: 'POST',
      url: '/ext/trigger_setup_tool/triggers/t9/trigger',
    })
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore))
  })

  it('创建表单：填消息提交（类型预选首项）→ 切类型再提交（body=工具入参契约）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)

    // 类型 select 值缺省时落首选项（RJSF/antd 既有语义）——delay 类型须带
    // delay_seconds（requiredWhen 条件必填），填消息+延迟秒数后可提交
    fireEvent.change(await screen.findByLabelText('触发消息'), { target: { value: '检查任务状态' } })
    fireEvent.change(screen.getByLabelText('延迟秒数'), { target: { value: '60' } })
    fireEvent.submit((screen.getByLabelText('触发消息') as HTMLElement).closest('form')!)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    let body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body).toMatchObject({ trigger_type: 'delay', message: '检查任务状态', delay_seconds: 60 })

    // 切类型（antd Select：mouseDown 展开 → 点选项）→ 提交体跟随切换；
    // interval 类型须带 interval（requiredWhen 条件必填）
    fireEvent.mouseDown(screen.getByRole('combobox', { name: /类型/ }))
    await waitFor(() => expect(screen.getByText('周期（按间隔重复）')).toBeInTheDocument())
    fireEvent.click(screen.getByText('周期（按间隔重复）'))
    fireEvent.change(screen.getByLabelText('周期间隔'), { target: { value: '5m' } })
    fireEvent.submit((screen.getByLabelText('触发消息') as HTMLElement).closest('form')!)
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(2))
    const [url, init] = fetchMock.mock.calls[1]
    expect(url).toBe('/ext/trigger_setup_tool/triggers')
    body = JSON.parse((init as RequestInit).body as string)
    expect(body).toMatchObject({ trigger_type: 'interval', message: '检查任务状态', interval: '5m' })
    vi.unstubAllGlobals()
  })

  it('条件必填提醒：类型=延迟缺延迟秒数 → 提交被拦且字段级提示可见', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)

    fireEvent.change(await screen.findByLabelText('触发消息'), { target: { value: '检查任务状态' } })
    fireEvent.submit((screen.getByLabelText('触发消息') as HTMLElement).closest('form')!)
    expect(await screen.findByText('延迟秒数不能为空')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('无激活管道 → 创建表单提供「新建会话」入口，模态框保存走 createSession 流', async () => {
    const createSessionMock = vi.fn().mockResolvedValue({ id: 's1', title: '触发目标会话' })
    useSessionListStore.setState({ createSession: createSessionMock })
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)

    fireEvent.click(await screen.findByRole('button', { name: '新建会话' }))
    // 桩化模态框暴露保存按钮 → 驱动 onSave 回调（title/agent/插件表单产物）
    fireEvent.click(screen.getByTestId('mock-session-modal-save'))
    await waitFor(() =>
      expect(createSessionMock).toHaveBeenCalledWith('触发目标会话', {
        agentId: undefined,
        fieldMetadata: {},
      }),
    )
  })

  it('已有激活管道 → 不再显示「新建会话」入口', async () => {
    usePipelineMessageStore.setState({ activePipelineId: 'p-active' })
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)
    await screen.findByLabelText('触发消息')
    expect(screen.queryByRole('button', { name: '新建会话' })).not.toBeInTheDocument()
  })

  it('管道下拉选项拉 /ext/trigger_setup_tool/pipelines（options 信封）', async () => {
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/ext/trigger_setup_tool/pipelines'),
    )
  })
})
