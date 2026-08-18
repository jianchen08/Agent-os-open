/**
 * widget_demo 插件契约测试（全特性端到端）
 *
 * 直接读取 plugins/shared/system/widget_demo/plugin.json，按生产链路装载
 * （loadFromSchema / loadChatCardDeclarations / loadOutputSchemas）后：
 * - 渲染 WidgetStage（space=widget-demo）断言全部声明 widget 可渲染可交互
 * - 覆盖 T1 fields / T2+T3 chat_card / T4 output_schema / G1-G6 全机制
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import fs from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { WidgetStage } from '../WidgetStage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { toFormFields } from '@/utils/configFormFields'
import { clearChatCardDeclarations, interpretChatCard, loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { buildOutputSchemaView, clearOutputSchemas, loadOutputSchemas } from '@/utils/outputSchemaView'

const PLUGIN_JSON = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../../../../plugins/shared/system/widget_demo/plugin.json'),
    'utf-8',
  ),
) as {
  id: string
  name: string
  capabilities: { tools: Array<Record<string, unknown>> }
  config_files: Array<Record<string, unknown>>
  ui_schema: { widgets: Array<Record<string, unknown>> }
  contributes: { pages: Array<Record<string, unknown>> }
}

const apiGet = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

function seedRegistry() {
  const tools = PLUGIN_JSON.capabilities.tools.map((t) => ({ name: t.name, ui: t.ui, output_schema: t.output_schema }))
  contributionRegistry.loadFromSchema({
    agents: [
      { id: PLUGIN_JSON.id, ui_schema: PLUGIN_JSON.ui_schema },
    ],
    plugin_configs: [
      { plugin_id: PLUGIN_JSON.id, plugin_name: PLUGIN_JSON.name, config_files: PLUGIN_JSON.config_files },
    ],
    plugin_contributes: [
      { plugin_id: PLUGIN_JSON.id, plugin_name: PLUGIN_JSON.name, contributes: PLUGIN_JSON.contributes },
    ],
  })
  loadChatCardDeclarations(tools as never)
  loadOutputSchemas(tools as never)
}

/** 定位包含指定字段 label 的 form（演示台多表单共存，submit 必须打目标表单） */
const formOf = (label: string) => (screen.getByLabelText(label) as HTMLElement).closest('form')!

beforeEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  clearChatCardDeclarations()
  clearOutputSchemas()
  apiGet.mockReset()
  apiRequest.mockReset()
  // 默认 mock：datasource/fieldsUri/dataUri 的 GET 返回空形状，防渲染树崩
  apiGet.mockResolvedValue({ data: {} })
  initializeWidgets()
  seedRegistry()
})

describe('widget_demo 插件契约', () => {
  it('contributes.pages 声明经 openWorkspacePanelByPath 可打开演示台（widget_stage）', () => {
    const page = contributionRegistry.getPages().find((p) => p.id === 'widget_demo')
    expect(page).toBeDefined()
    expect(page?.path).toBe('/widget-demo')
    expect(page?.widget).toBe('widget_stage')
    expect(page?.slot).toBe('activity-bar')
  })

  it('T1：config_files.fields 收敛为类型化字段（toggle/select/slider/multiselect/textarea）', () => {
    const fields = toFormFields(PLUGIN_JSON.config_files[0].fields as never)
    const byName = Object.fromEntries(fields.map((f) => [f.name, f]))
    expect(byName.enabled.type).toBe('toggle')
    expect(byName.level.type).toBe('select')
    expect(byName.threshold.type).toBe('slider')
    expect(byName.tags.type).toBe('multiselect')
    expect(byName.note.type).toBe('textarea')
  })

  it('演示台渲染全部声明 widget（compact/级联/轮询/发射/watch/向导/排序/内联/受控）', () => {
    render(<WidgetStage space="widget-demo" />)
    for (const id of [
      'demo_form_compact',
      'demo_form_cascade',
      'demo_form_data',
      'demo_form_emit',
      'demo_panel_watch',
      'demo_wizard',
      'demo_sortable',
      'demo_inline',
      'demo_controlled',
    ]) {
      expect(screen.getByTestId(`declared-widget-${id}`)).toBeInTheDocument()
    }
  })

  it('G4 受控桥：demo_controlled compact 按钮反映宿主值；排序/内联经受控注入', async () => {
    render(<WidgetStage space="widget-demo" />)
    // compact 受控：按钮显示宿主当前值「中」
    expect(screen.getByRole('button', { name: '中' })).toBeInTheDocument()
    // 排序（宿主受控）：点击下移 alpha → 宿主状态更新 → 顺序变化
    fireEvent.click(screen.getByRole('button', { name: '下移 alpha' }))
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('beta')
    // 内联（宿主受控）：点击编辑 → 输入 → Enter → 视图显示新值
    fireEvent.click(screen.getByTestId('inline-edit-view'))
    fireEvent.change(screen.getByTestId('inline-edit-input'), { target: { value: '新值' } })
    fireEvent.keyDown(screen.getByTestId('inline-edit-input'), { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('inline-edit-view')).toHaveTextContent('新值'))
  })

  it('G2+G6-a：级联 datasourceUri 走 /api/v1/datasource/ 代理并随提供商重拉', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/v1/datasource/widget_demo/options/')) {
        return Promise.resolve({ data: { options: [{ label: 'm1', value: 'm1' }] } })
      }
      return Promise.reject(new Error(`unexpected: ${url}`))
    })
    render(<WidgetStage space="widget-demo" />)
    // 非绝对 datasourceUri → 代理前缀（G6-a 真实路由在 kernel 侧；前端路径断言）
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/api/v1/datasource/widget_demo/options/regions'),
    )
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/datasource/widget_demo/options/models?provider='),
      ),
    )
  })

  it('G6-b：轮询表单挂载即拉 fieldsUri+dataUri（/ext/widget_demo/*）', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/ext/widget_demo/schema') return Promise.resolve({ data: { fields: [{ name: 'enabled', type: 'toggle', label: '启用' }] } })
      if (url === '/ext/widget_demo/config') return Promise.resolve({ data: { enabled: true, fetch_count: 1 } })
      return Promise.resolve({ data: {} })
    })
    render(<WidgetStage space="widget-demo" />)
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/widget_demo/config'))
  })

  it('G3 事件联动端到端：emit 表单提交 → watch 面板重新 GET /state', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockImplementation((url: string) => {
      if (url === '/ext/widget_demo/schema') return Promise.resolve({ data: { fields: [{ name: 'x', type: 'input', label: 'X' }] } })
      if (url === '/ext/widget_demo/state') return Promise.resolve({ data: { submit_count: 0 } })
      return Promise.resolve({ data: {} })
    })
    render(<WidgetStage space="widget-demo" />)
    // watch 面板初始 GET /state
    await waitFor(() => expect(apiGet.mock.calls.filter((c) => c[0] === '/ext/widget_demo/state')).toHaveLength(1))
    // emit 表单提交 → 事件 → watch 面板重挂载 → 再次 GET /state
    fireEvent.change(screen.getByLabelText('任务标题'), { target: { value: 'T1' } })
    fireEvent.submit(formOf('任务标题'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/ext/widget_demo/actions/submit', expect.anything()))
    await waitFor(() =>
      expect(apiGet.mock.calls.filter((c) => c[0] === '/ext/widget_demo/state').length).toBeGreaterThan(1),
    )
    vi.unstubAllGlobals()
  })

  it('G5 向导：分步前进 + 末步提交全量累积值', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
    render(<WidgetStage space="widget-demo" />)
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'W1' } })
    fireEvent.submit(formOf('标题'))
    await waitFor(() => expect(screen.getByText('2. 执行环境')).toBeInTheDocument())
    // 步骤 2 表单异步挂载——用 findByLabelText 等渲染完成
    const wsInput = (await screen.findByLabelText('工作空间')) as HTMLElement
    fireEvent.change(wsInput, { target: { value: 'ws-1' } })
    fireEvent.submit(wsInput.closest('form')!)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toMatchObject({ title: 'W1', workspace: 'ws-1', pipeline_id: expect.any(String) })
    vi.unstubAllGlobals()
  })

  it('T2+T3：chat_card form 块 + actions 协议（copy/open_url）', () => {
    const decl = PLUGIN_JSON.capabilities.tools[0].ui.chat_card as never
    const out = interpretChatCard(decl, { args: { summary: 'hello demo' }, result: { duration_ms: 42, status: 'ok' } })
    // form 块
    const formBlock = out.details.find((d) => d.contentType === 'form')
    expect(formBlock).toBeDefined()
    expect((formBlock?.content as Record<string, unknown>).endpoint).toBe('/ext/widget_demo/actions/submit')
    // actions：copy + open_url 均已接线（非禁用）
    expect(out.actions).toHaveLength(2)
    expect(out.actions.every((a) => !a.disabled)).toBe(true)
    expect(out.actions[0].confirmMessage).toBe('复制摘要到剪贴板？')
  })

  it('T4：output_schema 契约视图（合规零违规 / 违规标警）', () => {
    const schema = PLUGIN_JSON.capabilities.tools[1].output_schema as Record<string, unknown>
    const ok = buildOutputSchemaView(schema, { status: 'ok', score: 9 })
    expect(ok).not.toBeNull()
    expect(ok!.violations).toEqual([])
    expect(ok!.block.contentType).toBe('form')
    const bad = buildOutputSchemaView(schema, { status: 'bogus' })
    expect(bad!.violations.join('\n')).toContain('not in enum')
  })
})
