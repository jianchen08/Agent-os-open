// @feature: FP-0.2.四 前端 Schema（Widget Action Bus） | @ci: frontend-test
/**
 * Widget Action Bus 测试（ADR 2026-10-10-widget-action-bus）
 *
 * 覆盖三件 API 与分发链：
 * - useWidgetAction 发意图 → 内置域 host adapter 收到（≥2 组区分度动作）；
 * - 未声明动作 / 词表外动作 / 无分发宿主 = 显式 reject（错误处理铁律）；
 * - host adapter 同名重复注册报错（不静默遮蔽）；
 * - declareWidgetActions 词表进 WidgetRegistry 元数据；
 * - actionUri 分发：apiClient 请求面（uri/method/payload）+ 成功联动事件
 *   （{widgetId}:{actionName}，复用 formEventBus watch 语义）+ 失败传播。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { subscribeFormEvent } from '@/services/schema/formEventBus'
import {
  WidgetActionContext,
  clearWidgetHostAdapters,
  declareWidgetActions,
  registerWidgetHostAdapter,
  resolveWidgetAction,
  useWidgetAction,
} from '@/services/schema/widgetActionBus'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'

const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (..._args: unknown[]) => {
      throw new Error('actionUri 分发须走 apiClient.request，不走裸调用')
    },
    {
      request: (...args: unknown[]) => apiRequest(...args),
      get: () => {
        throw new Error('unexpected apiClient.get in action bus tests')
      },
    },
  ),
}))

/** 用 useWidgetAction 的探针组件：按钮点击发指定动作（记录 reject 供断言） */
const actionProbeErrors: Array<{ action: string; message: string }> = []
function makeActionProbe(actionName: string): WidgetComponent {
  const Probe = () => {
    const trigger = useWidgetAction(actionName)
    return (
      <button type="button" data-testid={`probe-${actionName}`} onClick={() => {
        void trigger({ source: 'probe', action: actionName }).catch((err: unknown) => {
          actionProbeErrors.push({
            action: actionName,
            message: err instanceof Error ? err.message : String(err),
          })
        })
      }}>
        fire {actionName}
      </button>
    )
  }
  return Probe as WidgetComponent
}

describe('widgetActionBus — 内置域 host adapter 分发（经 DeclaredWidgetLayer）', () => {
  beforeEach(() => {
    clearWidgetHostAdapters()
    widgetRegistry.clear()
    contributionRegistry.clear()
    apiRequest.mockReset()
    actionProbeErrors.length = 0
  })
  afterEach(() => {
    clearWidgetHostAdapters()
    widgetRegistry.clear()
    contributionRegistry.clear()
    vi.restoreAllMocks()
  })

  it('useWidgetAction 发意图 → 宿主 adapter 收到（两组区分度动作+payload 透传）', async () => {
    const received: Array<{ name: string; payload: unknown }> = []
    registerWidgetHostAdapter('pipeline.open', (payload) => {
      received.push({ name: 'pipeline.open', payload })
      return { opened: true }
    })
    registerWidgetHostAdapter('task.create', (payload) => {
      received.push({ name: 'task.create', payload })
      return { id: 't-1' }
    })
    widgetRegistry.register('panel_x', makeActionProbe('pipeline.open'), {
      name: 'panel_x',
      supportedSpaces: ['workspace'],
    })
    widgetRegistry.register('panel_y', makeActionProbe('task.create'), {
      name: 'panel_y',
      supportedSpaces: ['workspace'],
    })
    // 词表声明（组件只能发声明过的动作）
    declareWidgetActions('panel_x', { 'pipeline.open': { description: '打开管道' } })
    declareWidgetActions('panel_y', { 'task.create': { description: '建任务' } })

    render(
      <DeclaredWidgetLayer
        declarations={[
          { id: 'px', type: 'panel_x', space: 'workspace' },
          { id: 'py', type: 'panel_y', space: 'workspace' },
        ]}
        space="workspace"
      />,
    )

    fireEvent.click(screen.getByTestId('probe-pipeline.open'))
    fireEvent.click(screen.getByTestId('probe-task.create'))
    await waitFor(() => expect(received).toHaveLength(2))

    expect(received[0]).toEqual({
      name: 'pipeline.open',
      payload: { source: 'probe', action: 'pipeline.open' },
    })
    expect(received[1]).toEqual({
      name: 'task.create',
      payload: { source: 'probe', action: 'task.create' },
    })
    // 探针无 reject 记录 = 两动作均成功 resolve
    expect(actionProbeErrors).toEqual([])
  })

  it('词表外动作：adapter 存在但 declareWidgetActions 未声明 → 显式 reject', async () => {
    registerWidgetHostAdapter('pipeline.open', () => ({ opened: true }))
    widgetRegistry.register('panel_z', makeActionProbe('pipeline.open'), {
      name: 'panel_z',
      supportedSpaces: ['workspace'],
    })
    // 故意不 declareWidgetActions

    render(
      <DeclaredWidgetLayer
        declarations={[{ id: 'pz', type: 'panel_z', space: 'workspace' }]}
        space="workspace"
      />,
    )
    fireEvent.click(screen.getByTestId('probe-pipeline.open'))

    await waitFor(() => expect(actionProbeErrors).toHaveLength(1))
    expect(actionProbeErrors[0].action).toBe('pipeline.open')
    expect(actionProbeErrors[0].message).toContain('动作词表')
  })

  it('未声明动作（无 adapter、无 actionUri）→ 显式 reject「未声明」', async () => {
    widgetRegistry.register('panel_w', makeActionProbe('nope.action'), {
      name: 'panel_w',
      supportedSpaces: ['workspace'],
    })
    render(
      <DeclaredWidgetLayer
        declarations={[{ id: 'pw', type: 'panel_w', space: 'workspace' }]}
        space="workspace"
      />,
    )
    fireEvent.click(screen.getByTestId('probe-nope.action'))

    await waitFor(() => expect(actionProbeErrors).toHaveLength(1))
    expect(actionProbeErrors[0].message).toContain('未声明')
  })

  it('组件渲染在 DeclaredWidgetLayer 之外（无 WidgetActionContext）→ 显式 reject', async () => {
    const Probe = makeActionProbe('pipeline.open')
    render(<Probe />)
    fireEvent.click(screen.getByTestId('probe-pipeline.open'))

    await waitFor(() => expect(actionProbeErrors).toHaveLength(1))
    expect(actionProbeErrors[0].message).toContain('无分发宿主')
  })

  it('host adapter 同名重复注册 → 报错（不静默遮蔽）', () => {
    registerWidgetHostAdapter('dup.act', () => 1)
    expect(() => registerWidgetHostAdapter('dup.act', () => 2)).toThrow(/重复注册即遮蔽/)
  })

  it('adapter 返回值经 dispatch resolve 传回组件（动作结果可消费）', async () => {
    let resolved: unknown = null
    registerWidgetHostAdapter('pipeline.open', (payload) => ({ echo: payload }))
    widgetRegistry.register('panel_r', makeActionProbe('pipeline.open'), {
      name: 'panel_r',
      supportedSpaces: ['workspace'],
    })
    declareWidgetActions('panel_r', { 'pipeline.open': {} })
    render(
      <DeclaredWidgetLayer
        declarations={[{ id: 'pr', type: 'panel_r', space: 'workspace' }]}
        space="workspace"
      />,
    )
    // 直接经 resolveWidgetAction 断言返回值链（组件侧由上面探针覆盖）
    resolved = await resolveWidgetAction(
      'pipeline.open',
      { id: 'pr', type: 'panel_r', pluginId: 'test' },
      { x: 1 },
    )
    expect(resolved).toEqual({ echo: { x: 1 } })
  })
})

describe('widgetActionBus — declareWidgetActions 词表进 WidgetRegistry 元数据', () => {
  beforeEach(() => widgetRegistry.clear())
  afterEach(() => {
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('声明面落在注册条目元数据（合并追加，不覆盖既有词表）', () => {
    widgetRegistry.register('pipeline_manager', () => null, {
      name: '管道管理',
      supportedSpaces: ['workspace'],
      actions: { 'pipeline.refresh': { description: '刷新管道列表' } },
    })
    declareWidgetActions('pipeline_manager', { 'pipeline.open': { description: '打开管道' } })

    const meta = widgetRegistry.getEntry('pipeline_manager')?.metadata.actions
    expect(Object.keys(meta ?? {}).sort()).toEqual(['pipeline.open', 'pipeline.refresh'])
    expect(meta?.['pipeline.open']?.description).toBe('打开管道')
  })

  it('未注册 type 声明动作词表 → 显式报错（不允许悬空词表）', () => {
    expect(() => declareWidgetActions('ghost_type', { 'a.b': {} })).toThrow(/未注册/)
  })
})

describe('widgetActionBus — actionUri 分发（第三方直调插件端点）', () => {
  beforeEach(() => {
    clearWidgetHostAdapters()
    apiRequest.mockReset()
    apiRequest.mockResolvedValue({ data: { ok: true, result: 42 } })
  })
  afterEach(() => {
    clearWidgetHostAdapters()
    vi.restoreAllMocks()
  })

  const decl = {
    id: 'cost_panel',
    type: 'status_card',
    pluginId: 'cost_control',
    actions: {
      reset: { uri: '/ext/cost_control/report/reset', method: 'POST' },
    },
  }

  it('请求面正确：uri/method/payload 透传，resolve 插件响应体', async () => {
    const out = await resolveWidgetAction('reset', decl, { month: '2026-10' })
    expect(out).toEqual({ ok: true, result: 42 })
    expect(apiRequest).toHaveBeenCalledWith({
      url: '/ext/cost_control/report/reset',
      method: 'POST',
      data: { month: '2026-10' },
    })
  })

  it('成功后发 formEventBus 联动事件 {widgetId}:{actionName}（payload=响应体，复用 watch 语义）', async () => {
    const seen: Array<{ event: string; payload: unknown }> = []
    const off = subscribeFormEvent('cost_panel:reset', (payload) => {
      seen.push({ event: 'cost_panel:reset', payload })
    })
    try {
      await resolveWidgetAction('reset', decl, undefined)
      expect(seen).toEqual([{ event: 'cost_panel:reset', payload: { ok: true, result: 42 } }])
    } finally {
      off()
    }
  })

  it('请求失败向上传播（错误处理铁律：不吞），不发联动事件', async () => {
    apiRequest.mockRejectedValueOnce(new Error('插件端点 502'))
    const seen: unknown[] = []
    const off = subscribeFormEvent('cost_panel:reset', (p) => seen.push(p))
    try {
      await expect(resolveWidgetAction('reset', decl, undefined)).rejects.toThrow('插件端点 502')
      expect(seen).toEqual([])
    } finally {
      off()
    }
  })

  it('无 payload 时 data 为 undefined（GET 形动作不带请求体）', async () => {
    const declGet = {
      id: 'g1',
      type: 'status_card',
      pluginId: 'p',
      actions: { fetch: { uri: '/ext/p/dump', method: 'GET' } },
    }
    await resolveWidgetAction('fetch', declGet, undefined)
    expect(apiRequest).toHaveBeenCalledWith({
      url: '/ext/p/dump',
      method: 'GET',
      data: undefined,
    })
  })
})

describe('widgetActionContext — 分发函数注入形态', () => {
  it('context 值形态：declaration + dispatch（DeclaredWidgetLayer 逐 widget 注入）', async () => {
    const captured: Array<{ action: string; payload: unknown }> = []
    const declaration = {
      id: 'ctx1',
      type: 'status_card',
      pluginId: 'p1',
      actions: { go: { uri: '/ext/p1/go', method: 'POST' } },
    }
    const Probe = () => {
      const trigger = useWidgetAction('go')
      return (
        <button
          type="button"
          data-testid="ctx-probe"
          onClick={() => void trigger({ n: 1 }).catch(() => captured.push({ action: 'go-reject', payload: null }))}
        >
          go
        </button>
      )
    }
    render(
      <WidgetActionContext.Provider
        value={{
          declaration,
          dispatch: (actionName, payload) => {
            captured.push({ action: actionName, payload })
            return Promise.resolve('done')
          },
        }}
      >
        <Probe />
      </WidgetActionContext.Provider>,
    )
    fireEvent.click(screen.getByTestId('ctx-probe'))
    await waitFor(() => expect(captured).toEqual([{ action: 'go', payload: { n: 1 } }]))
  })
})
