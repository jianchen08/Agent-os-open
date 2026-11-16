/** @feature FP-T12 chat.toolview keyed 槽 | @ci: frontend-test */
/**
 * chat.toolview keyed 槽测试（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md
 * §4.4「消息流渲染器通道规则」）
 *
 * 契约：
 * - 内建卡词表白名单不变：词表内卡型照旧；词表外非命名空间卡型声明即弃
 *   （回落数据路由，生成闸不变禁手抄）
 * - 插件自定义卡出口：render.card 值 = `<pluginId>/<name>` 形态被接受，
 *   renderIntentToBlocks 产出 JSON 透传块（args/result），DetailBlock 按
 *   卡型键路由 ToolviewCardHost
 * - keyed 槽寻址：chat.toolview 槽条目按卡型键解析；component 通道 widget
 *   收到声明 props + 工具数据；webview 通道懒加载
 * - fail-closed：槽未装载/条目缺失 = 可见占位不静默空白；widget 炸 =
 *   ErrorBoundary 隔离
 */

import { render, screen } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ToolviewCardHost } from '@/components/chat/ToolviewCardHost'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import {
  addRenderIntent,
  getRenderIntent,
  isNamespacedRenderCard,
  loadRenderIntents,
  renderIntentToBlocks,
  type RenderContext,
} from '@/utils/renderIntent'
import type { ActivityDetailBlock } from '@/types/activity'

vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (props: Record<string, unknown>) => (
    <div data-testid="webview-stub" data-html-path={String(props.htmlPath ?? '')} />
  ),
}))

const ctx: RenderContext = {
  args: { path: '/tmp/x.py' },
  result: { ok: 1 },
  error: null,
  duration_ms: 5,
}

beforeEach(() => {
  widgetRegistry.clear()
  slotRegistry.clear()
})

describe('render.card 词表出口', () => {
  it('内建词表白名单不变：词表内卡型照旧、词表外非命名空间即弃', () => {
    loadRenderIntents([
      { name: 'tool_ok', render: { card: 'terminal' } },
      { name: 'tool_bad', render: { card: 'made_up_card' } },
    ])
    expect(getRenderIntent('tool_ok')?.card).toBe('terminal')
    // 词表外且非命名空间（无 "/"）→ 弃（落数据路由）
    expect(getRenderIntent('tool_bad')).toBeUndefined()
  })

  it('插件自定义卡出口：`<pluginId>/<name>` 形态被接受', () => {
    expect(isNamespacedRenderCard('myplug/my_card')).toBe(true)
    expect(isNamespacedRenderCard('myplug/a/b')).toBe(false)
    expect(isNamespacedRenderCard('terminal')).toBe(false)
    addRenderIntent('tool_custom', { card: 'myplug/my_card' })
    expect(getRenderIntent('tool_custom')?.card).toBe('myplug/my_card')
  })

  it('renderIntentToBlocks：命名空间卡产出 JSON 透传块（contentType=卡型键）', () => {
    const blocks = renderIntentToBlocks({ card: 'myplug/my_card' }, ctx)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].contentType).toBe('myplug/my_card')
    expect(blocks[0].content).toEqual({ args: { path: '/tmp/x.py' }, result: { ok: 1 } })
  })

  it('renderIntentToBlocks：内建卡不受出口影响（terminal 仍走字段族映射）', () => {
    const blocks = renderIntentToBlocks(
      { card: 'terminal' },
      { args: { command: 'ls' }, result: { output: 'file.py', exit_code: 0 }, error: null, duration_ms: 5 },
    )
    expect(blocks[0].contentType).toBe('terminal')
    expect(blocks[0].terminal).toBeDefined()
  })
})

describe('ToolviewCardHost（chat.toolview keyed 槽）', () => {
  async function loadCardEntry(entry: Record<string, unknown>): Promise<void> {
    await slotRegistry.loadFromSchema(
      { plugin_contributes: [{ plugin_id: 'myplug', contributes: { slots: [entry] } }] },
      { loadWidgets: vi.fn().mockResolvedValue([]) },
    )
  }

  const block: ActivityDetailBlock = {
    id: 'toolview:myplug/my_card',
    label: 'myplug/my_card',
    content: { args: { q: 1 }, result: { data: 'r' } },
    contentType: 'myplug/my_card',
  }

  it('component 通道：widget 收到声明 props + 工具数据（args/result）', async () => {
    const seen: Record<string, unknown> = {}
    const CardWidget = (props: Record<string, unknown>): React.ReactElement => {
      Object.assign(seen, props)
      return <div data-testid="toolview-card-ok">卡内容</div>
    }
    widgetRegistry.register('myplug/my_card', CardWidget, { name: 'myplug/my_card' })
    await loadCardEntry({
      id: 'mycard',
      slot: 'chat.toolview',
      key: 'myplug/my_card',
      channel: 'component',
      widget: 'myplug/my_card',
      props: { theme: 'auto' },
    })
    render(<ToolviewCardHost card="myplug/my_card" block={block} />)
    expect(screen.getByTestId('toolview-card-ok')).toBeInTheDocument()
    expect(seen.theme).toBe('auto')
    expect(seen.card).toBe('myplug/my_card')
    expect(seen.args).toEqual({ q: 1 })
    expect(seen.result).toEqual({ data: 'r' })
  })

  it('槽条目缺失（插件禁用/未装载）→ 可见占位不静默空白', () => {
    render(<ToolviewCardHost card="myplug/ghost_card" block={block} />)
    expect(screen.getByTestId('toolview-card-missing')).toBeInTheDocument()
    expect(screen.getByTestId('toolview-card-missing').textContent).toContain('myplug/ghost_card')
  })

  it('widget 渲染炸 → ErrorBoundary 隔离（可见占位，不拖塌消息列表）', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const Boom = (): never => {
      throw new Error('card exploded')
    }
    widgetRegistry.register('myplug/boom', Boom, { name: 'myplug/boom' })
    await loadCardEntry({
      id: 'boom',
      slot: 'chat.toolview',
      key: 'myplug/boom',
      channel: 'component',
      widget: 'myplug/boom',
    })
    render(<ToolviewCardHost card="myplug/boom" block={block} />)
    expect(screen.getByTestId('slot-entry-error')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('webview 通道：懒加载 webview 沙箱承载（htmlPath 随槽条目）', async () => {
    await loadCardEntry({
      id: 'heavy',
      slot: 'chat.toolview',
      key: 'myplug/heavy_view',
      channel: 'webview',
      htmlPath: '/card/index.html',
    })
    render(<ToolviewCardHost card="myplug/heavy_view" block={block} />)
    const stub = await screen.findByTestId('webview-stub')
    expect(stub.getAttribute('data-html-path')).toBe('/card/index.html')
  })
})
