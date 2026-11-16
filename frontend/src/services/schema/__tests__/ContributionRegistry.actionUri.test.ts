// @feature: FP-0.2.四 前端 Schema（actionUri 白名单声明期校验） | @ci: frontend-test
/**
 * actionUri 白名单声明期校验测试（ADR 2026-10-10-widget-action-bus §3）
 *
 * 判定时机 = extractWidgets 解析 ui_schema.widgets 时逐条校验；
 * 处置粒度 = action 项（拒绝注册 + console.error，不拆整个 widget）。
 * fail-closed 性质：凡 method+path 不落在该插件自己 routes 面内的动作，
 * 无论越界方向（他插件端点 / 不存在端点 / method 不符 / 路径多段），一律
 * 拒绝注册且可观测。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { registerWidgetHostAdapter, clearWidgetHostAdapters } from '@/services/schema/widgetActionBus'
import type { SchemaResponse } from '@/services/api/schema'

/** 构造带 routes 面 + 单插件 ui_schema widget（actions 可覆写）的 schema */
function makeSchema(actions: Record<string, unknown>): SchemaResponse {
  return {
    agents: [],
    pipelines: [],
    tools: [],
    routes: {
      cost_control: [
        { route_id: 'r1', method: 'POST', path: '/ext/cost_control/report/reset' },
        { route_id: 'r2', method: 'GET', path: '/ext/cost_control/budget/status' },
        { route_id: 'r3', method: 'DELETE', path: '/ext/cost_control/table/{table}' },
      ],
      other_plugin: [
        { route_id: 'o1', method: 'POST', path: '/ext/other_plugin/report/reset' },
      ],
    },
    plugin_contributes: [
      {
        plugin_id: 'cost_control',
        plugin_name: 'cost',
        contributes: {},
        ui_schema: {
          widgets: [{ id: 'cost_panel', type: 'status_card', props: {}, actions }],
        },
      },
    ],
  } as unknown as SchemaResponse
}

describe('ContributionRegistry — actionUri 白名单（声明期注册校验）', () => {
  let registry: ContributionRegistry
  let errorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    registry = new ContributionRegistry()
    clearWidgetHostAdapters()
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    clearWidgetHostAdapters()
    vi.restoreAllMocks()
  })

  const actionsOf = (): Record<string, unknown> =>
    (registry.getWidgetsForPlugin('cost_control')[0]?.actions ?? {}) as Record<string, unknown>

  it('合法：落在该插件 routes 面内（精确匹配 + method 缺省 POST）通过注册', () => {
    registry.loadFromSchema(
      makeSchema({ reset: { uri: '/ext/cost_control/report/reset' } }),
    )
    expect(actionsOf()).toEqual({
      reset: { uri: '/ext/cost_control/report/reset', method: 'POST' },
    })
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('合法：{param} 模板段允许实例化（实例化段按模板段匹配）', () => {
    registry.loadFromSchema(
      makeSchema({ drop: { uri: '/ext/cost_control/table/sessions', method: 'DELETE' } }),
    )
    expect(actionsOf()).toEqual({
      drop: { uri: '/ext/cost_control/table/sessions', method: 'DELETE' },
    })
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('合法：method 小写归一为面内大写（post == POST）', () => {
    registry.loadFromSchema(
      makeSchema({ reset: { uri: '/ext/cost_control/report/reset', method: 'post' } }),
    )
    expect(actionsOf().reset).toEqual({
      uri: '/ext/cost_control/report/reset',
      method: 'POST',
    })
  })

  it('合法：label/description 展示提示透传（display-only，不影响判定）', () => {
    registry.loadFromSchema(
      makeSchema({ reset: { uri: '/ext/cost_control/report/reset', label: '重置', description: '清零' } }),
    )
    expect(actionsOf().reset).toEqual({
      uri: '/ext/cost_control/report/reset',
      method: 'POST',
      label: '重置',
      description: '清零',
    })
  })

  it('越界：他插件端点 / 不存在端点 / method 不符 / 模板多一段 —— 全部拒绝注册+错误日志', () => {
    registry.loadFromSchema(
      makeSchema({
        steal: { uri: '/ext/other_plugin/report/reset', method: 'POST' }, // 他插件端点
        ghost: { uri: '/ext/cost_control/no_such/route', method: 'POST' }, // 不存在端点
        wrongMethod: { uri: '/ext/cost_control/report/reset', method: 'GET' }, // method 不符
        extraSeg: { uri: '/ext/cost_control/table/t/extra', method: 'DELETE' }, // 实例化段多一段
        kernel: { uri: '/api/v1/tasks', method: 'GET' }, // 内核端点
      }),
    )
    expect(actionsOf()).toEqual({})
    // fail-closed 性质断言：每个越界方向都有一条错误日志，全部可归因
    const msgs = errorSpy.mock.calls.map((c) => String(c[0]))
    for (const m of msgs) expect(m).toContain('越界拒绝注册')
    expect(msgs.some((m) => m.includes('steal'))).toBe(true)
    expect(msgs.some((m) => m.includes('ghost'))).toBe(true)
    expect(msgs.some((m) => m.includes('wrongMethod'))).toBe(true)
    expect(msgs.some((m) => m.includes('extraSeg'))).toBe(true)
    expect(msgs.some((m) => m.includes('kernel'))).toBe(true)
  })

  it('粒度 = action 项：一个越界动作不拆整个 widget（合法项与数据面保留）', () => {
    registry.loadFromSchema(
      makeSchema({
        reset: { uri: '/ext/cost_control/report/reset' },
        steal: { uri: '/ext/other_plugin/report/reset', method: 'POST' },
      }),
    )
    const widgets = registry.getWidgetsForPlugin('cost_control')
    expect(widgets).toHaveLength(1)
    expect(widgets[0].type).toBe('status_card')
    expect(widgets[0].props).toEqual({})
    expect(actionsOf()).toEqual({ reset: { uri: '/ext/cost_control/report/reset', method: 'POST' } })
  })

  it('routes 面无该插件条目 → 全部动作拒绝（fail-closed，空面不放行）', () => {
    const schema = makeSchema({ reset: { uri: '/ext/cost_control/report/reset' } })
    delete (schema.routes as Record<string, unknown>).cost_control
    registry.loadFromSchema(schema)
    expect(actionsOf()).toEqual({})
    expect(errorSpy).toHaveBeenCalled()
  })

  it('形态非法：非对象 actions / 条目缺 uri / 相对路径 uri → 拒绝注册+错误日志', () => {
    registry.loadFromSchema(makeSchema({ bad: { method: 'POST' } } as Record<string, unknown>))
    expect(actionsOf()).toEqual({})
    expect(errorSpy).toHaveBeenCalled()

    registry.loadFromSchema(makeSchema({ rel: { uri: 'ext/cost_control/report/reset' } }))
    expect(actionsOf()).toEqual({})
    expect(errorSpy).toHaveBeenCalled()
  })

  it('actions 声明为非对象（数组）→ 整体忽略+错误日志，widget 本体不受影响', () => {
    registry.loadFromSchema(
      makeSchema([{ uri: '/ext/cost_control/report/reset' }] as unknown as Record<string, unknown>),
    )
    const widgets = registry.getWidgetsForPlugin('cost_control')
    expect(widgets).toHaveLength(1)
    expect(widgets[0].actions).toBeUndefined()
    expect(errorSpy).toHaveBeenCalled()
  })

  it('与内置域 host adapter 同名冲突 → 拒绝注册+错误日志（不静默遮蔽）', () => {
    registerWidgetHostAdapter('reset', () => null)
    registry.loadFromSchema(makeSchema({ reset: { uri: '/ext/cost_control/report/reset' } }))
    expect(actionsOf()).toEqual({})
    const msgs = errorSpy.mock.calls.map((c) => String(c[0]))
    expect(msgs.some((m) => m.includes('同名冲突'))).toBe(true)
  })

  it('无 actions 声明 → declaration.actions 缺省（既有声明零影响）', () => {
    registry.loadFromSchema(makeSchema({}))
    const widgets = registry.getWidgetsForPlugin('cost_control')
    expect(widgets).toHaveLength(1)
    expect(widgets[0].actions).toBeUndefined()
    expect(errorSpy).not.toHaveBeenCalled()
  })
})
