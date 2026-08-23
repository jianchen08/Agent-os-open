/**
 * triggers 触发器声明契约测试（B1：TriggersPage 声明化替代）
 *
 * 读 trigger_setup_tool plugin.json（生产声明，channel_api 退役后随域迁入）
 * → 装载 → 渲染 widget_stage space=triggers：断言 table 行操作
 * （trigger/enable/disable when 分支/delete）+ 创建 form 的字段/提交，
 * 全部走声明链路。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import fs from 'node:fs'
import path from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { WidgetStage } from '@/components/schema/widgets/WidgetStage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'

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
  it('声明存在：triggers_table（rowActions）与 triggers_create（form）', () => {
    const widgets = contributionRegistry.getAllWidgets()
    const table = widgets.find((w) => w.id === 'triggers_table')
    expect(table?.type).toBe('table')
    const props = table?.props as { rowActions?: unknown[] } | undefined
    expect(props?.rowActions).toHaveLength(4)
    const page = contributionRegistry.getPages().find((p) => p.id === 'triggers')
    expect(page?.path).toBe('/triggers')
    expect(page?.widget).toBe('widget_stage')
  })

  it('列表渲染 + when 分支：启用按钮只在 enabled=false 行出现、禁用反之', async () => {
    apiGet.mockResolvedValue({
      data: {
        columns: [
          { key: 'id', label: 'ID' },
          { key: 'name', label: '名称' },
          { key: 'enabled', label: '启用' },
        ],
        rows: [
          { id: 't1', name: '定时器', enabled: true },
          { id: 't2', name: '手动器', enabled: false },
        ],
      },
    })
    render(<WidgetStage space="triggers" />)
    await waitFor(() => expect(screen.getByText('定时器')).toBeInTheDocument())
    // 行1(enabled=true)：有禁用、无启用；行2(enabled=false)：有启用、无禁用
    const disableBtns = screen.getAllByRole('button', { name: '禁用' })
    expect(disableBtns).toHaveLength(1)
    const enableBtns = screen.getAllByRole('button', { name: '启用' })
    expect(enableBtns).toHaveLength(1)
    // 触发/删除两行都有
    expect(screen.getAllByRole('button', { name: '触发' })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: '删除' })).toHaveLength(2)
  })

  it('点击「触发」→ POST {id} 模板端点 → 成功后重拉', async () => {
    apiGet.mockResolvedValue({
      data: { columns: [{ key: 'id', label: 'ID' }], rows: [{ id: 't9', enabled: false }] },
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

  it('创建表单：字段渲染 + 提交 POST /ext/trigger_setup_tool/triggers', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockResolvedValue({ data: { columns: [], rows: [] } })
    render(<WidgetStage space="triggers" />)
    fireEvent.change(await screen.findByLabelText('名称'), { target: { value: '新触发器' } })
    fireEvent.submit((screen.getByLabelText('名称') as HTMLElement).closest('form')!)
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/ext/trigger_setup_tool/triggers')
    expect(JSON.parse(init.body)).toMatchObject({ name: '新触发器', type: 'schedule' })
    vi.unstubAllGlobals()
  })
})
