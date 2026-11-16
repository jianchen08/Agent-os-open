// @feature: FP-0.2.四 前端 Schema（actionUri 第三方形态） | @ci: frontend-test
/**
 * actionUri 第三方形态测试（ADR 2026-10-10 验收硬项）
 *
 * 模拟一个仅带 ui_schema 声明（actionUri 指向自己 /ext 端点）的第三方插件
 * schema：前端零插件代码——schema 装载（声明期白名单校验）→ DeclaredWidgetLayer
 * 渲染 → status_card 通用件零代码渲染可点按钮 → 点击 → apiClient 请求落在
 * 声明端点面（mock apiClient 层断言 uri/method）→ 失败内联可见（fail-closed
 * 面向用户同样可见）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { SchemaResponse } from '@/services/api/schema'

const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    () => {
      throw new Error('unexpected apiClient call')
    },
    { request: (...args: unknown[]) => apiRequest(...args) },
  ),
}))

/** 第三方插件 schema：仅 ui_schema 声明 + 自己的 routes 面，前端无任何专属代码 */
function thirdPartySchema(): SchemaResponse {
  return {
    agents: [],
    pipelines: [],
    tools: [],
    routes: {
      third_party_report: [
        { route_id: 'tp1', method: 'POST', path: '/ext/third_party_report/report/reset' },
        { route_id: 'tp2', method: 'GET', path: '/ext/third_party_report/report/refresh' },
      ],
    },
    plugin_contributes: [
      {
        plugin_id: 'third_party_report',
        plugin_name: '第三方报表',
        contributes: {},
        ui_schema: {
          widgets: [
            {
              id: 'cost_panel',
              type: 'status_card',
              space: 'workspace',
              props: { title: '季度成本', value: 42 },
              actions: {
                reset: { uri: '/ext/third_party_report/report/reset', label: '重置统计' },
                refresh: { uri: '/ext/third_party_report/report/refresh', method: 'GET' },
              },
            },
          ],
        },
      },
    ],
  } as unknown as SchemaResponse
}

describe('actionUri 第三方形态（插件 ui_schema 声明 → 前端零代码按钮直调）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    apiRequest.mockReset()
    apiRequest.mockResolvedValue({ data: { ok: true } })
    // 生产同款注册（registerWidgets 对 status_card 的注册形态）
    widgetRegistry.register('status_card', StatusCardWidget, {
      name: '状态卡',
      supportedSpaces: ['chat', 'workspace', 'floating'],
    })
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('零代码渲染按钮：声明 actions → status_card 出可点按钮（label 优先，缺省动作名）', () => {
    contributionRegistry.loadFromSchema(thirdPartySchema())

    render(<DeclaredWidgetLayer space="workspace" />)

    expect(screen.getByTestId('declared-widget-cost_panel')).toBeInTheDocument()
    expect(screen.getByTestId('widget-action-reset')).toHaveTextContent('重置统计')
    expect(screen.getByTestId('widget-action-refresh')).toHaveTextContent('refresh')
  })

  it('点击发对请求面：POST 重置 + GET 刷新，两动作各自落声明端点', async () => {
    contributionRegistry.loadFromSchema(thirdPartySchema())
    render(<DeclaredWidgetLayer space="workspace" />)

    fireEvent.click(screen.getByTestId('widget-action-reset'))
    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(1))
    expect(apiRequest).toHaveBeenNthCalledWith(1, {
      url: '/ext/third_party_report/report/reset',
      method: 'POST',
      data: undefined,
    })

    fireEvent.click(screen.getByTestId('widget-action-refresh'))
    await waitFor(() => expect(apiRequest).toHaveBeenCalledTimes(2))
    expect(apiRequest).toHaveBeenNthCalledWith(2, {
      url: '/ext/third_party_report/report/refresh',
      method: 'GET',
      data: undefined,
    })
  })

  it('请求失败内联可见（role=alert），不吞错误也不炸组件', async () => {
    apiRequest.mockRejectedValueOnce(new Error('plugin endpoint 502'))
    contributionRegistry.loadFromSchema(thirdPartySchema())
    render(<DeclaredWidgetLayer space="workspace" />)

    fireEvent.click(screen.getByTestId('widget-action-reset'))

    expect(await screen.findByRole('alert')).toHaveTextContent('plugin endpoint 502')
    // 组件仍在（失败不卸载 widget）
    expect(screen.getByTestId('declared-widget-cost_panel')).toBeInTheDocument()
  })

  it('越界 action 在声明期被拒：越界项无按钮（越界方向=他插件端点），合法项保留', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const schema = thirdPartySchema()
    const widgets = (
      schema.plugin_contributes[0].ui_schema?.widgets ?? []
    ) as Array<Record<string, unknown>>
    widgets[0].actions = {
      reset: { uri: '/ext/third_party_report/report/reset' },
      steal: { uri: '/ext/cost_control/report/reset', method: 'POST' },
    }
    contributionRegistry.loadFromSchema(schema)

    render(<DeclaredWidgetLayer space="workspace" />)

    expect(screen.getByTestId('widget-action-reset')).toBeInTheDocument()
    expect(screen.queryByTestId('widget-action-steal')).not.toBeInTheDocument()
    expect(errorSpy).toHaveBeenCalled()
  })
})
