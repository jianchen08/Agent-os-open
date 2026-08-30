/**
 * ShortcutRegistry 测试（P5-c 快捷键，ADR §3.4 档位二）
 *
 * 快捷键绑定：manifest 声明 { command, key, when }，前端注册全局快捷键，
 * 命中 when 时触发对应 command（经 CommandDispatcher）。
 *
 * 不 Mock 被测逻辑（ShortcutRegistry 本体）；jsdom 不派发真实键盘事件，
 * 故直接测 normalizeKey + shouldFire + matchKey 的纯逻辑。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { ShortcutRegistry, normalizeKey } from '@/services/schema/shortcutRegistry'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'

describe('normalizeKey — 键序列归一化', () => {
  it('大小写字母归一为小写', () => {
    expect(normalizeKey('Ctrl+S')).toBe('ctrl+s')
    expect(normalizeKey('ctrl+s')).toBe('ctrl+s')
  })

  it('多修饰键顺序无关', () => {
    expect(normalizeKey('Ctrl+Shift+P')).toBe('ctrl+shift+p')
    expect(normalizeKey('Shift+Ctrl+P')).toBe('ctrl+shift+p')
  })

  it('空格容错', () => {
    expect(normalizeKey('Ctrl + S')).toBe('ctrl+s')
  })

  it('Mac 修饰键 Cmd 映射为 meta', () => {
    expect(normalizeKey('Cmd+S')).toBe('meta+s')
  })
})

describe('ShortcutRegistry — 注册与匹配', () => {
  let registry: ShortcutRegistry
  let contrib: ContributionRegistry

  beforeEach(() => {
    contrib = new ContributionRegistry()
    registry = new ShortcutRegistry(contrib)
    useContextKeys.getState().reset()
  })

  it('从 contributes.shortcuts 加载快捷键绑定', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'editor',
          contributes: {
            shortcuts: [
              { command: 'editor.save', key: 'Ctrl+S', when: 'workspace.focus' },
              { command: 'editor.find', key: 'Ctrl+F', when: 'workspace.focus' },
            ],
          },
        },
      ],
    })
    registry.refresh()

    const bindings = registry.getBindings()
    expect(bindings).toHaveLength(2)
    expect(bindings.map((b) => b.command).sort()).toEqual(['editor.find', 'editor.save'])
  })

  it('matchKey 按 KeyboardEvent 归一化后查找 command', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'e', contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S', when: 'workspace.focus' }] } },
      ],
    })
    registry.refresh()

    // 模拟 KeyboardEvent
    const ev = { ctrlKey: true, shiftKey: false, altKey: false, metaKey: false, key: 's' }
    expect(registry.matchKey(ev as unknown as KeyboardEvent)).toBe('e.save')

    const evNo = { ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, key: 's' }
    expect(registry.matchKey(evNo as unknown as KeyboardEvent)).toBeUndefined()
  })

  it('matchKey 对 key 为 undefined 的事件不崩溃（扩展/自动化派发的非标准事件）', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'e', contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S', when: 'workspace.focus' }] } },
      ],
    })
    registry.refresh()

    // 真实浏览器中扩展/远程输入可能派发 key 为 undefined 的 keydown
    const evUndefined = { ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, key: undefined }
    expect(() => registry.matchKey(evUndefined as unknown as KeyboardEvent)).not.toThrow()
    expect(registry.matchKey(evUndefined as unknown as KeyboardEvent)).toBeUndefined()

    // 空串 key（另一类非标准形态）同样不命中
    const evEmpty = { ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, key: '' }
    expect(() => registry.matchKey(evEmpty as unknown as KeyboardEvent)).not.toThrow()
    expect(registry.matchKey(evEmpty as unknown as KeyboardEvent)).toBeUndefined()
  })

  it('when 失配时不触发（shouldFire 返回 false）', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'e', contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S', when: 'workspace.focus' }] } },
      ],
    })
    registry.refresh()

    // workspace.focus 默认 false
    expect(registry.shouldFire('e.save')).toBe(false)

    useContextKeys.getState().setWorkspaceFocus(true)
    expect(registry.shouldFire('e.save')).toBe(true)
  })

  it('refresh 幂等（重复加载不重复注册）', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'e', contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S', when: 'workspace.focus' }] } },
      ],
    })
    registry.refresh()
    registry.refresh()
    expect(registry.getBindings()).toHaveLength(1)
  })

  it('无 when 的快捷键恒可触发', () => {
    ;(contrib as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'e', contributes: { shortcuts: [{ command: 'e.global', key: 'F1' }] } },
      ],
    })
    registry.refresh()
    expect(registry.shouldFire('e.global')).toBe(true)
  })
})
