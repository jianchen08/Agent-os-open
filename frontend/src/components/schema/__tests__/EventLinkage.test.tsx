/**
 * 事件联动测试（widget 化 G3）
 *
 * 覆盖：formEventBus 发布订阅；FormWidget eventName 提交成功/失败发事件；
 * EventWatchBox 订阅触发重挂载（重拉语义）；DeclaredWidgetLayer watch 声明
 * 接线（端到端：表单 A 提交 → 表单 B watch 自动重载 datasource）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import {
  emitFormEvent,
  subscribeFormEvent,
} from '@/services/schema/formEventBus'
import { EventWatchBox } from '@/components/schema/EventWatchBox'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'

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

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
  // 声明 type='form' 需 FormWidget 已注册（生产链路由 initializeWidgets 提供）
  initializeWidgets()
})

describe('formEventBus', () => {
  it('发布/订阅/退订/去重', () => {
    const h1 = vi.fn()
    const h2 = vi.fn()
    const unsub1 = subscribeFormEvent('task.created', h1)
    subscribeFormEvent('task.created', h2)
    subscribeFormEvent('task.created', h1) // 同 handler 重复订阅去重
    emitFormEvent('task.created', { id: 1 })
    expect(h1).toHaveBeenCalledTimes(1)
    expect(h2).toHaveBeenCalledTimes(1)
    expect(h1).toHaveBeenCalledWith({ id: 1 })
    unsub1()
    emitFormEvent('task.created', { id: 2 })
    expect(h1).toHaveBeenCalledTimes(1) // 已退订不再收到
    expect(h2).toHaveBeenCalledTimes(2) // 另一订阅者仍收到
  })

  it('订阅者抛错不阻断其他订阅者', () => {
    subscribeFormEvent('x', () => {
      throw new Error('boom')
    })
    const ok = vi.fn()
    subscribeFormEvent('x', ok)
    expect(() => emitFormEvent('x', 1)).not.toThrow()
    expect(ok).toHaveBeenCalledTimes(1)
  })
})

describe('FormWidget eventName 发射', () => {
  it('提交成功 emit {eventName}(payload=表单值)', async () => {
    const sub = vi.fn()
    subscribeFormEvent('task.created', sub)
    render(
      <FormWidget
        eventName="task.created"
        fields={[{ name: 'title', type: 'input' as const, label: '标题' }]}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    submitForm()
    await waitFor(() => expect(sub).toHaveBeenCalledWith(expect.objectContaining({ title: 'T1' })))
  })

  it('提交失败 emit {eventName}:failed(payload={error, values})', async () => {
    const sub = vi.fn()
    subscribeFormEvent('task.created:failed', sub)
    render(
      <FormWidget
        eventName="task.created"
        fields={[{ name: 'title', type: 'input' as const, label: '标题' }]}
        onSubmit={vi.fn().mockRejectedValue(new Error('no agent'))}
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(sub).toHaveBeenCalled())
    const payload = sub.mock.calls[0][0] as { error: string; values: Record<string, unknown> }
    expect(payload.error).toBe('no agent')
    expect(payload.values.title).toBe('T')
  })
})

describe('EventWatchBox', () => {
  it('事件触发 → 重挂载子组件（挂载 spy 计数 +1）', async () => {
    const mountSpy = vi.fn()
    render(
      <EventWatchBox watch={{ event: 'task.created', action: 'reload' }}>
        {(reloadKey) => <ChildMountSpy key={reloadKey} mountSpy={mountSpy} reloadKey={reloadKey} />}
      </EventWatchBox>,
    )
    expect(mountSpy).toHaveBeenCalledTimes(1)
    emitFormEvent('task.created', {})
    await waitFor(() => expect(mountSpy).toHaveBeenCalledTimes(2))
    // 非 watch 事件不触发
    emitFormEvent('other.event', {})
    await new Promise((r) => setTimeout(r, 50))
    expect(mountSpy).toHaveBeenCalledTimes(2)
  })
})

function ChildMountSpy({ mountSpy, reloadKey }: { mountSpy: () => void; reloadKey: number }) {
  mountSpy()
  return <div data-testid="child-spy">{reloadKey}</div>
}

describe('DeclaredWidgetLayer watch 接线（声明级联动）', () => {
  it('声明 props.watch 的 widget 在事件触发后重挂载（reload-key 递增）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ task_id: 't-1' }) })
    vi.stubGlobal('fetch', fetchMock)
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          ui_schema: {
            widgets: [
              {
                id: 'form_a',
                type: 'form',
                space: 'workspace',
                props: {
                  eventName: 'task.created',
                  endpoint: '/ext/tasks/root',
                  fields: [{ name: 'title', type: 'input', label: '标题' }],
                },
              },
              {
                id: 'panel_b',
                type: 'form',
                space: 'workspace',
                props: {
                  watch: [{ event: 'task.created', action: 'reload' }],
                  fields: [{ name: 'note', type: 'input', label: '备注' }],
                },
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<DeclaredWidgetLayer space="workspace" />)
    expect(screen.getByTestId('declared-widget-form_a')).toBeInTheDocument()
    const panelB = screen.getByTestId('declared-widget-panel_b')
    expect(panelB.getAttribute('data-reload-key')).toBe('0')
    // 表单 A 提交 → 事件 → 面板 B 重挂载
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() =>
      expect(screen.getByTestId('declared-widget-panel_b').getAttribute('data-reload-key')).toBe('1'),
    )
    vi.unstubAllGlobals()
  })

  it('端到端：A 提交 → B(datasource) watch 自动重拉（重新 GET）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ task_id: 't-1' }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockImplementation((url: string) =>
      Promise.resolve({
        data:
          url.endsWith('/schema')
            ? { fields: [{ name: 'note', type: 'input', label: '备注' }] }
            : { data: { note: 'v1' } },
      }),
    )
    apiRequest.mockResolvedValue({ data: {} })
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          ui_schema: {
            widgets: [
              {
                id: 'form_a',
                type: 'form',
                space: 'workspace',
                props: {
                  eventName: 'task.created',
                  endpoint: '/ext/tasks/root',
                  fields: [{ name: 'title', type: 'input', label: '标题' }],
                },
              },
              {
                id: 'form_b',
                type: 'form',
                space: 'workspace',
                props: {
                  watch: [{ event: 'task.created', action: 'reload' }],
                  fieldsUri: '/ext/agent_manager/agents/schema',
                  dataUri: '/ext/agent_manager/agents/x/config',
                  dataFormat: 'json',
                },
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<DeclaredWidgetLayer space="workspace" />)
    // B 初始加载（GET schema + config）
    await waitFor(() => expect(apiGet.mock.calls.filter((c) => c[0].endsWith('/config'))).toHaveLength(1))
    const getsBefore = apiGet.mock.calls.length
    // A 提交成功 → emit task.created → B watch reload → 重新 GET config
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore))
    expect(apiGet.mock.calls.some((c) => c[0].endsWith('/config'))).toBe(true)
    vi.unstubAllGlobals()
  })
})

describe('G6-b：定时轮询刷新（refresh poll）', () => {
  it('RefreshBox 按 interval 重挂载子组件', async () => {
    const { RefreshBox } = await import('@/components/schema/RefreshBox')
    const mountSpy = vi.fn()
    render(
      <RefreshBox refresh={{ type: 'poll', intervalSeconds: 1 }}>
        {(key) => <ChildMountSpy key={key} mountSpy={mountSpy} reloadKey={key} />}
      </RefreshBox>,
    )
    expect(mountSpy).toHaveBeenCalledTimes(1)
    await new Promise((r) => setTimeout(r, 1150))
    expect(mountSpy).toHaveBeenCalledTimes(2)
    await new Promise((r) => setTimeout(r, 1100))
    expect(mountSpy).toHaveBeenCalledTimes(3)
  })

  it('声明 refresh 的 widget 定时重拉 datasource（重新 GET）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ task_id: 't-1' }) })
    vi.stubGlobal('fetch', fetchMock)
    apiGet.mockImplementation((url: string) =>
      Promise.resolve({
        data:
          url.endsWith('/schema')
            ? { fields: [{ name: 'note', type: 'input', label: '备注' }] }
            : { data: { note: 'v1' } },
      }),
    )
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          ui_schema: {
            widgets: [
              {
                id: 'form_b',
                type: 'form',
                space: 'workspace',
                props: {
                  refresh: { type: 'poll', intervalSeconds: 1 },
                  fieldsUri: '/ext/agent_manager/agents/schema',
                  dataUri: '/ext/agent_manager/agents/x/config',
                  dataFormat: 'json',
                },
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<DeclaredWidgetLayer space="workspace" />)
    await waitFor(() => expect(apiGet.mock.calls.some((c) => c[0].endsWith('/config'))).toBe(true))
    const getsBefore = apiGet.mock.calls.length
    // 等一个轮询周期 → 重挂载 → 重新 GET config
    await new Promise((r) => setTimeout(r, 1150))
    expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore)
    vi.unstubAllGlobals()
  })
})
