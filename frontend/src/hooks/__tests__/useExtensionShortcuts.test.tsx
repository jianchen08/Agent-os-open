/**
 * useExtensionShortcuts hook 测试（P5-c）
 *
 * 在组件挂载时注册全局 keydown 监听，命中快捷键（when 通过）时
 * 经 CommandDispatcher 触发命令。
 *
 * jsdom 支持构造 KeyboardEvent 并 dispatch，故真实模拟键盘。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useExtensionShortcuts } from '@/hooks/useExtensionShortcuts'
import { ShortcutRegistry } from '@/services/schema/shortcutRegistry'
import { CommandDispatcher } from '@/services/schema/commandDispatcher'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'

function fireKey(opts: KeyboardEventInit): void {
  document.dispatchEvent(new KeyboardEvent('keydown', opts))
}

describe('useExtensionShortcuts — 全局快捷键监听', () => {
  let contrib: ContributionRegistry
  let shortcuts: ShortcutRegistry
  let dispatcher: CommandDispatcher

  beforeEach(() => {
    contrib = new ContributionRegistry()
    shortcuts = new ShortcutRegistry(contrib)
    dispatcher = new CommandDispatcher(contrib)
    useContextKeys.getState().reset()
  })

  it('挂载后注册 keydown 监听；命中快捷键触发 command', () => {
    contrib.loadFromSchema({
      modules: [
        {
          module_id: 'e',
          contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S', when: 'workspace.focus' }] },
        },
      ],
    } as never)
    shortcuts.refresh()

    const execute = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(execute)

    renderHook(() => useExtensionShortcuts(shortcuts, dispatcher))

    // workspace.focus 默认 false → 不触发
    fireKey({ key: 's', ctrlKey: true })
    expect(execute).not.toHaveBeenCalled()

    // 启用 workspace.focus 后触发
    useContextKeys.getState().setWorkspaceFocus(true)
    fireKey({ key: 's', ctrlKey: true })
    expect(execute).toHaveBeenCalledWith('e.save', undefined)
  })

  it('输入框聚焦时不触发（避免编辑时拦截）', () => {
    contrib.loadFromSchema({
      modules: [
        { module_id: 'e', contributes: { shortcuts: [{ command: 'e.save', key: 'Ctrl+S' }] } },
      ],
    } as never)
    shortcuts.refresh()

    const execute = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(execute)

    renderHook(() => useExtensionShortcuts(shortcuts, dispatcher))

    // 模拟输入框聚焦
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()

    fireKey({ key: 's', ctrlKey: true })
    expect(execute).not.toHaveBeenCalled()

    // 判空移除：避免 StrictMode 双挂载/竞态下节点已被移除时 removeChild 抛 NotFoundError
    if (input.parentNode) input.parentNode.removeChild(input)
  })

  it('无 when 的快捷键恒触发', () => {
    contrib.loadFromSchema({
      modules: [
        { module_id: 'e', contributes: { shortcuts: [{ command: 'e.help', key: 'F1' }] } },
      ],
    } as never)
    shortcuts.refresh()

    const execute = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(execute)

    renderHook(() => useExtensionShortcuts(shortcuts, dispatcher))
    fireKey({ key: 'F1' })
    expect(execute).toHaveBeenCalledWith('e.help', undefined)
  })

  it('卸载时移除监听', () => {
    contrib.loadFromSchema({
      modules: [
        { module_id: 'e', contributes: { shortcuts: [{ command: 'e.help', key: 'F1' }] } },
      ],
    } as never)
    shortcuts.refresh()
    const execute = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(execute)

    const { unmount } = renderHook(() => useExtensionShortcuts(shortcuts, dispatcher))
    unmount()
    fireKey({ key: 'F1' })
    expect(execute).not.toHaveBeenCalled()
  })
})
