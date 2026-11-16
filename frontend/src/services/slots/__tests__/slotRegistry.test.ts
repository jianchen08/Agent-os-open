/** @feature FP-T12 前端槽位扩展机制 | @ci: frontend-test */
/**
 * SlotRegistry 测试（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 契约：
 * - contributes.slots 首类声明装载（list 聚合排序 / keyed 按键）
 * - 存量收敛翻译：contributes.pages(space=workspace)→workspace.tab 条目、
 *   contributes.modal→shell.overlay 条目（触发面 openOn 保留）
 * - fail-closed：表外槽 key / 未开放键 / channel 缺字段 / keyed 缺 key /
 *   component 通道引用未注册 widget = 装载期报错跳过该条目（不静默空白）
 * - 插件禁用语义：schema 重载不含该插件 → 其条目消失（幂等重载）
 * - 装载错误可查询（getLoadErrors，管理可见原因的取数面）
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import {
  slotRegistry,
  type SlotEntry,
  type SlotLoadError,
} from '../slotRegistry'
import type { ComponentType } from 'react'

function FakeWidget(): null {
  return null
}

function registerBuiltins(names: string[]): void {
  for (const n of names) {
    widgetRegistry.register(n, FakeWidget as ComponentType, { name: n })
  }
}

/** 构造 plugin_contributes 入口项 */
function contrib(
  pluginId: string,
  contributes: Record<string, unknown>,
): { plugin_id: string; contributes: Record<string, unknown> } {
  return { plugin_id: pluginId, contributes }
}

type Schema = {
  plugin_contributes?: Array<{ plugin_id: string; contributes?: Record<string, unknown> }>
}

async function load(schema: Schema): Promise<void> {
  // 单测无真实动态装载：注入 no-op loader（W 链路在 widgetLoader/代表性测试覆盖）
  await slotRegistry.loadFromSchema(schema, { loadWidgets: vi.fn().mockResolvedValue(undefined) })
}

const errorsFor = (errors: SlotLoadError[], pluginId: string): SlotLoadError[] =>
  errors.filter((e) => e.pluginId === pluginId)

beforeEach(() => {
  widgetRegistry.clear()
  slotRegistry.clear()
})

describe('SlotRegistry 装载 contributes.slots', () => {
  it('list 槽聚合多条目并按 order 稳定排序（缺省 order 殿后）', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'late', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 20, title: '后' },
            { id: 'early', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 10, title: '先' },
            { id: 'default', slot: 'sidebar.block', channel: 'component', widget: 'status_card', title: '缺省' },
          ],
        }),
      ],
    })
    const entries = slotRegistry.getSlotEntries('sidebar.block')
    expect(entries.map((e) => e.id)).toEqual(['plug_a:early', 'plug_a:late', 'plug_a:default'])
    expect(entries[0].pluginId).toBe('plug_a')
    expect(entries[0].kind).toBe('list')
  })

  it('keyed 槽按键取条目（workspace.tab 语义：key=path）', async () => {
    registerBuiltins(['widget_stage'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            {
              id: 'tab1',
              slot: 'workspace.tab',
              key: '/p/tab1',
              channel: 'component',
              widget: 'widget_stage',
              title: '页签一',
              props: { space: 'x' },
            },
          ],
        }),
      ],
    })
    const hit = slotRegistry.getKeyedEntry('workspace.tab', '/p/tab1')
    expect(hit?.widget).toBe('widget_stage')
    expect(hit?.title).toBe('页签一')
    expect(slotRegistry.getKeyedEntry('workspace.tab', '/p/missing')).toBeUndefined()
  })

  it('when 条目原样携带（求值在渲染侧，注册表存声明）', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'w', slot: 'sidebar.block', channel: 'component', widget: 'status_card', when: "user.role == 'admin'" },
          ],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('sidebar.block')[0]?.when).toBe("user.role == 'admin'")
  })
})

describe('SlotRegistry 存量收敛翻译', () => {
  it('contributes.pages(space=workspace) → workspace.tab keyed 条目（key=path??id）', async () => {
    registerBuiltins(['widget_stage', 'webview'])
    await load({
      plugin_contributes: [
        contrib('monitoring', {
          pages: [
            {
              id: 'tool_call_records',
              title: '工具调用记录',
              space: 'workspace',
              slot: 'tab',
              path: '/p/tool_call_records',
              order: 62,
              widget: 'webview',
              props: { pluginId: 'monitoring', htmlPath: '/page/tool-calls' },
            },
            {
              id: 'nav_btn',
              title: '监控',
              space: 'workspace',
              slot: 'activity-bar',
              path: '/monitoring',
              widget: 'widget_stage',
            },
          ],
        }),
      ],
    })
    // tab 声明 → workspace.tab 条目
    const tab = slotRegistry.getKeyedEntry('workspace.tab', '/p/tool_call_records')
    expect(tab?.widget).toBe('webview')
    expect(tab?.title).toBe('工具调用记录')
    // activity-bar 条目也是 space=workspace → 可按键（path）解析为页签内容
    const nav = slotRegistry.getKeyedEntry('workspace.tab', '/monitoring')
    expect(nav?.widget).toBe('widget_stage')
    // 非 workspace 空间的 pages 不翻译（settings/debug_center 域不进首批槽）
    await load({
      plugin_contributes: [
        contrib('llm', {
          pages: [{ id: 's', title: '设置页', space: 'settings', widget: 'x_widget' }],
        }),
      ],
    })
    expect(slotRegistry.getKeyedEntry('workspace.tab', '/p/s')).toBeUndefined()
  })

  it('contributes.modal → shell.overlay 条目（触发面 openOn 保留，findOverlayByTrigger 命中）', async () => {
    registerBuiltins(['form'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          modal: [
            {
              id: 'pick_thing',
              title: '选择',
              openOn: 'on_command:plug_a.pick',
              widget: 'form',
              props: { k: 1 },
            },
          ],
        }),
      ],
    })
    const overlay = slotRegistry.findOverlayByTrigger('on_command:plug_a.pick')
    expect(overlay?.widget).toBe('form')
    expect(overlay?.channel).toBe('component')
    expect(slotRegistry.getSlotEntries('shell.overlay')).toHaveLength(1)
    expect(slotRegistry.findOverlayByTrigger('on_command:other')).toBeUndefined()
  })
})

describe('SlotRegistry fail-closed（装载期报错跳过，AC-F2）', () => {
  it('表外槽 key → 报错跳过', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [{ id: 'bad', slot: 'made.up.slot', channel: 'component', widget: 'status_card' }],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('made.up.slot')).toHaveLength(0)
    const errs = errorsFor(slotRegistry.getLoadErrors(), 'plug_a')
    expect(errs).toHaveLength(1)
    expect(errs[0].entryId).toContain('bad')
  })

  it('词表内但未开放（statusbar.item 悬置）→ 报错跳过', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [{ id: 'sb', slot: 'statusbar.item', channel: 'component', widget: 'status_card' }],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('statusbar.item')).toHaveLength(0)
    expect(errorsFor(slotRegistry.getLoadErrors(), 'plug_a')).toHaveLength(1)
  })

  it('component 通道缺 widget / webview 通道缺 htmlPath → 报错跳过', async () => {
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'no_widget', slot: 'sidebar.block', channel: 'component' },
            { id: 'no_html', slot: 'sidebar.block', channel: 'webview' },
          ],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(0)
    expect(errorsFor(slotRegistry.getLoadErrors(), 'plug_a')).toHaveLength(2)
  })

  it('keyed 槽缺 key → 报错跳过', async () => {
    registerBuiltins(['widget_stage'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [{ id: 'nokey', slot: 'workspace.tab', channel: 'component', widget: 'widget_stage' }],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('workspace.tab')).toHaveLength(0)
    expect(errorsFor(slotRegistry.getLoadErrors(), 'plug_a')).toHaveLength(1)
  })

  it('component 通道引用未注册 widget → 装载期报错跳过（声明↔注册一致性，AC-F5）', async () => {
    // widgetRegistry 无 status_card：装载期校验即拦，不渲染静默空白
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [{ id: 'ghost', slot: 'sidebar.block', channel: 'component', widget: 'ghost_widget' }],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(0)
    const errs = errorsFor(slotRegistry.getLoadErrors(), 'plug_a')
    expect(errs).toHaveLength(1)
    expect(errs[0].reason).toContain('ghost_widget')
  })

  it('槽条目非对象 / channel 非法 → 结构守卫报错跳过', async () => {
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            'not-an-object',
            { id: 'bad_channel', slot: 'sidebar.block', channel: 'iframe', widget: 'status_card' },
          ],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(0)
    const errs = errorsFor(slotRegistry.getLoadErrors(), 'plug_a')
    expect(errs).toHaveLength(2)
    const reasons = errs.map((e) => e.reason).join(' | ')
    expect(reasons).toContain('必须是对象')
    expect(reasons).toContain('channel 必须')
  })

  it('modal 条目缺 widget → 收敛翻译报错跳过（shell.overlay 无渲染体）', async () => {
    await load({
      plugin_contributes: [contrib('plug_a', { modal: [{ id: 'm1', title: '无体弹窗' }] })],
    })
    expect(slotRegistry.getSlotEntries('shell.overlay')).toHaveLength(0)
    const errs = errorsFor(slotRegistry.getLoadErrors(), 'plug_a')
    expect(errs).toHaveLength(1)
    expect(errs[0].reason).toContain('modal 条目缺少 widget')
  })

  it('坏条目跳过不影响同插件/他插件合法条目（粒度=条目）', async () => {
    registerBuiltins(['status_card', 'form'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'bad', slot: 'made.up.slot', channel: 'component', widget: 'status_card' },
            { id: 'good', slot: 'sidebar.block', channel: 'component', widget: 'status_card' },
          ],
        }),
        contrib('plug_b', {
          slots: [{ id: 'b1', slot: 'sidebar.block', channel: 'component', widget: 'form' }],
        }),
      ],
    })
    const entries = slotRegistry.getSlotEntries('sidebar.block')
    expect(entries.map((e) => e.id)).toEqual(['plug_a:good', 'plug_b:b1'])
  })
})

describe('SlotRegistry 生命周期', () => {
  it('插件禁用语义：重载 schema 不含该插件 → 条目消失（幂等重载防幽灵）', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [{ id: 's', slot: 'sidebar.block', channel: 'component', widget: 'status_card' }],
        }),
      ],
    })
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(1)
    await load({ plugin_contributes: [] })
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(0)
  })

  it('装载错误随重载清空；订阅版本号随装载推进', async () => {
    registerBuiltins(['status_card'])
    let version = slotRegistry.getVersion()
    let notified = 0
    const unsubscribe = slotRegistry.subscribe(() => {
      notified += 1
      version = slotRegistry.getVersion()
    })
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'bad', slot: 'nope', channel: 'component', widget: 'status_card' },
            { id: 'ok', slot: 'sidebar.block', channel: 'component', widget: 'status_card' },
          ],
        }),
      ],
    })
    expect(slotRegistry.getLoadErrors()).toHaveLength(1)
    expect(notified).toBeGreaterThan(0)
    expect(version).toBeGreaterThan(0)
    unsubscribe()
    slotRegistry.clear()
    expect(slotRegistry.getLoadErrors()).toHaveLength(0)
  })

  it('allEntries 汇总（调试/管理面取数）', async () => {
    registerBuiltins(['status_card'])
    await load({
      plugin_contributes: [
        contrib('plug_a', {
          slots: [
            { id: 'a', slot: 'sidebar.block', channel: 'component', widget: 'status_card' },
            { id: 'b', slot: 'shell.overlay', openOn: 'on_command:x', channel: 'component', widget: 'status_card' },
          ],
        }),
      ],
    })
    expect(slotRegistry.allEntries().map((e: SlotEntry) => e.id)).toEqual(['plug_a:a', 'plug_a:b'])
  })
})
