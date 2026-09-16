/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WidgetRegistry 分支补测（真实单例，非镜像类）
 *
 * 既有 WidgetRegistry.test.ts 断言的是仓库内一份镜像类副本，真实单例
 * （widgetRegistry）的 register/get/list/unregister/findFallback 覆盖不到。
 * 本文件直接操作真实单例，覆盖：注册元数据缺省归一化、按 space 过滤、
 * 降级映射表命中与断链（含 warn 只报一次）、size/unregister/clear 复位。
 *
 * 每个用例后 clear()，避免污染其它测试文件共享的全局单例。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import widgetRegistry from '../WidgetRegistry'
import type { WidgetComponent } from '../WidgetRegistry'

const Dummy = ((() => null) as unknown) as WidgetComponent

beforeEach(() => {
  widgetRegistry.clear()
})

afterEach(() => {
  widgetRegistry.clear()
  vi.restoreAllMocks()
})

describe('WidgetRegistry（真实单例） — register 归一化', () => {
  it('缺 name 时用 type 兜底，supportedSpaces 缺省为 chat+workspace', () => {
    widgetRegistry.register('chart', Dummy, {})
    const entry = widgetRegistry.getEntry('chart')
    expect(entry?.metadata.name).toBe('chart')
    expect(entry?.metadata.supportedSpaces).toEqual(['chat', 'workspace'])
  })

  it('显式 name/description/fallbackWidget 被保留', () => {
    widgetRegistry.register('table', Dummy, {
      name: '表格',
      description: '数据表',
      supportedSpaces: ['workspace'],
      fallbackWidget: 'status_card',
    })
    const entry = widgetRegistry.getEntry('table')
    expect(entry?.metadata).toMatchObject({
      name: '表格',
      description: '数据表',
      supportedSpaces: ['workspace'],
      fallbackWidget: 'status_card',
    })
  })

  it.each(['', '   '])('空/纯空白 type 抛错（type=%j）', (type) => {
    expect(() => widgetRegistry.register(type, Dummy, {})).toThrow('Widget type 不能为空')
  })

  it('同 type 重复注册为覆盖更新（后到者生效，size 不增）', () => {
    const Second = ((() => null) as unknown) as WidgetComponent
    widgetRegistry.register('dup', Dummy, { name: '第一次' })
    widgetRegistry.register('dup', Second, { name: '第二次' })
    expect(widgetRegistry.size).toBe(1)
    expect(widgetRegistry.getEntry('dup')?.metadata.name).toBe('第二次')
    expect(widgetRegistry.get('dup')).toBe(Second)
  })
})

describe('WidgetRegistry（真实单例） — 查询', () => {
  it('get 未注册返回 undefined；has 反映注册状态', () => {
    expect(widgetRegistry.get('ghost')).toBeUndefined()
    expect(widgetRegistry.has('ghost')).toBe(false)
    widgetRegistry.register('chart', Dummy, {})
    expect(widgetRegistry.get('chart')).toBe(Dummy)
    expect(widgetRegistry.has('chart')).toBe(true)
  })

  it('getEntry 未注册返回 undefined（entry 与组件同源）', () => {
    expect(widgetRegistry.getEntry('ghost')).toBeUndefined()
  })

  it('list 返回全部条目且随注册增长（性质：长度 === size）', () => {
    widgetRegistry.register('a', Dummy, {})
    widgetRegistry.register('b', Dummy, {})
    expect(widgetRegistry.list()).toHaveLength(widgetRegistry.size)
    expect(widgetRegistry.list().map((e) => e.type).sort()).toEqual(['a', 'b'])
  })

  it('listBySpace 只返回 supportedSpaces 含该空间的条目', () => {
    widgetRegistry.register('chat-only', Dummy, { supportedSpaces: ['chat'] })
    widgetRegistry.register('ws-only', Dummy, { supportedSpaces: ['workspace'] })
    widgetRegistry.register('both', Dummy, { supportedSpaces: ['chat', 'workspace'] })
    widgetRegistry.register('dock', Dummy, { supportedSpaces: ['dock'] })

    expect(widgetRegistry.listBySpace('chat').map((e) => e.type).sort()).toEqual(['both', 'chat-only'])
    expect(widgetRegistry.listBySpace('workspace').map((e) => e.type).sort()).toEqual(['both', 'ws-only'])
    expect(widgetRegistry.listBySpace('dock').map((e) => e.type)).toEqual(['dock'])
    expect(widgetRegistry.listBySpace('fullscreen')).toEqual([])
  })
})

describe('WidgetRegistry（真实单例） — findFallback 降级链', () => {
  it('精确匹配优先于降级表（已注册直接返回）', () => {
    const Kanban = ((() => null) as unknown) as WidgetComponent
    const Table = ((() => null) as unknown) as WidgetComponent
    widgetRegistry.register('kanban', Kanban, {})
    widgetRegistry.register('table', Table, {})
    expect(widgetRegistry.findFallback('kanban')).toBe(Kanban)
  })

  it.each([
    ['kanban', 'table'],
    ['editor', 'code_block'],
    ['terminal', 'code_block'],
    ['file_tree', 'table'],
    ['data_grid', 'table'],
    ['calendar', 'table'],
    ['log_stream', 'code_block'],
    ['topology', 'chart'],
    ['diff', 'code_block'],
    ['pivot', 'table'],
    ['dashboard', 'chart'],
    ['html_preview', 'code_block'],
    ['image_viewer', 'gallery'],
    ['tree', 'table'],
  ])('%s 未注册时降级到已注册的 %s', (type, fallbackType) => {
    const Fallback = ((() => null) as unknown) as WidgetComponent
    widgetRegistry.register(fallbackType, Fallback, {})
    expect(widgetRegistry.findFallback(type)).toBe(Fallback)
  })

  it('降级表候选链：首个候选缺席时用次级候选', () => {
    const StatusCard = ((() => null) as unknown) as WidgetComponent
    // kanban 候选链为 ['table','status_card']，只注册次级
    widgetRegistry.register('status_card', StatusCard, {})
    expect(widgetRegistry.findFallback('kanban')).toBe(StatusCard)
  })

  it('降级候选全缺时返回 undefined 且仅 warn 一次（同 type 不刷屏）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(widgetRegistry.findFallback('kanban')).toBeUndefined()
    expect(widgetRegistry.findFallback('kanban')).toBeUndefined()
    expect(warnSpy).toHaveBeenCalledTimes(1)
    expect(warnSpy.mock.calls[0][0]).toContain('kanban')
  })

  it('降级表外的未知 type 返回 undefined 并 warn 一次', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(widgetRegistry.findFallback('totally-unknown')).toBeUndefined()
    expect(warnSpy).toHaveBeenCalledTimes(1)
    expect(warnSpy.mock.calls[0][0]).toContain('totally-unknown')
  })

  it('不同未知 type 各自 warn（去重粒度是 type）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    widgetRegistry.findFallback('unknown-a')
    widgetRegistry.findFallback('unknown-b')
    widgetRegistry.findFallback('unknown-a')
    expect(warnSpy).toHaveBeenCalledTimes(2)
  })
})

describe('WidgetRegistry（真实单例） — unregister 与 clear', () => {
  it('unregister 已注册返回 true 并移除；重复 unregister 返回 false', () => {
    widgetRegistry.register('chart', Dummy, {})
    expect(widgetRegistry.unregister('chart')).toBe(true)
    expect(widgetRegistry.has('chart')).toBe(false)
    expect(widgetRegistry.unregister('chart')).toBe(false)
  })

  it('clear 清空条目与 size', () => {
    widgetRegistry.register('a', Dummy, {})
    widgetRegistry.register('b', Dummy, {})
    widgetRegistry.clear()
    expect(widgetRegistry.size).toBe(0)
    expect(widgetRegistry.list()).toEqual([])
  })

  it('clear 同时复位降级告警去重集合（清空后可再次告警）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    widgetRegistry.findFallback('kanban')
    expect(warnSpy).toHaveBeenCalledTimes(1)
    widgetRegistry.clear()
    widgetRegistry.findFallback('kanban')
    expect(warnSpy).toHaveBeenCalledTimes(2)
  })

  it('独立实例与单例互不影响（经单例构造器再实例化）', () => {
    // 模块只导出单例（类未单独导出），用单例的构造器取类
    const RegistryClass = widgetRegistry.constructor as new () => typeof widgetRegistry
    const other = new RegistryClass()
    other.register('only-other', Dummy, {})
    expect(other.has('only-other')).toBe(true)
    expect(widgetRegistry.has('only-other')).toBe(false)
    expect(other.size).toBe(1)
    expect(widgetRegistry.size).toBe(0)
  })
})
