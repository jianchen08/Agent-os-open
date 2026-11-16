/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ExtensionHost 覆盖缺口补测
 *
 * 契约：
 * - Cmd/Ctrl+Shift+P 切换命令面板开/关（非 Mac 用 ctrlKey；面板打开后再次快捷键关闭）
 * - 面板可见命令来自注入 dispatcher（真实 CommandDispatcher + 真实
 *   ContributionRegistry），点击命令执行（transport 收到 commandId）并关闭面板
 * - 命令触发 shell.overlay 槽条目（modal 收敛，C4 槽位机制）后渲染
 *   SlotOverlayHost；条目 widget 经 SlotRegistry 装载期 fail-closed——
 *   渲染期未注册（热禁用竞态窗口）显示可见占位；关闭按钮/遮罩关闭
 * - 未挂载时快捷键不生效（卸载后 keydown 不再打开面板）
 *
 * 不可达说明（逐条）：
 * 1. `isMac()` 的 `typeof navigator === 'undefined'` 分支要求运行环境无 navigator，
 *    jsdom 恒有 navigator，不可达（防御性 SSR 保护）。
 * 2. `renderModalContent` 的 `if (!modal) return null`：调用点前置 `{modal && (...)}`
 *    条件渲染，null 时 renderModalContent 不会被调用，不可达（类型收窄保护）。
 *
 * 测试策略：真实 ExtensionHost + 真实 CommandDispatcher/ContributionRegistry/
 * commandDispatcher 单例（注入 transport 与注册表），仅 transport 为外部边界
 * （内核调用）以 spy 注入。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ExtensionHost } from '@/components/extension/ExtensionHost'
import {
  CommandDispatcher,
  commandDispatcher,
} from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'

function makeDispatcher(contributes: Record<string, unknown[]>): CommandDispatcher {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [{ plugin_id: 'ext_demo', contributes }],
  } as never)
  const dispatcher = new CommandDispatcher(contributionRegistry)
  dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
  return dispatcher
}

/** 派发 Ctrl+Shift+P（isMac 在 jsdom 的非 Mac 环境判定为 false） */
function pressPaletteShortcut() {
  fireEvent.keyDown(document, { key: 'P', ctrlKey: true, shiftKey: true })
}

beforeEach(() => {
  contributionRegistry.clear()
  widgetRegistry.clear()
  slotRegistry.clear()
  vi.clearAllMocks()
})

describe('ExtensionHost — 命令面板', () => {
  it('Ctrl+Shift+P 打开面板并列出可见命令，再次快捷键关闭', () => {
    const dispatcher = makeDispatcher({
      commands: [{ id: 'demo.run', title: '运行演示', category: '演示' }],
    })
    render(<ExtensionHost dispatcher={dispatcher} />)

    expect(screen.queryByTestId('command-palette')).toBeNull()
    pressPaletteShortcut()
    expect(screen.getByTestId('command-palette')).toBeInTheDocument()
    expect(screen.getByText('运行演示')).toBeInTheDocument()

    pressPaletteShortcut()
    expect(screen.queryByTestId('command-palette')).toBeNull()
  })

  it('普通按键（无 Ctrl+Shift）不打开面板', () => {
    const dispatcher = makeDispatcher({ commands: [{ id: 'x', title: 'X' }] })
    render(<ExtensionHost dispatcher={dispatcher} />)
    fireEvent.keyDown(document, { key: 'P' })
    fireEvent.keyDown(document, { key: 'p', ctrlKey: true })
    expect(screen.queryByTestId('command-palette')).toBeNull()
  })

  it('点击命令经 transport 执行并关闭面板', async () => {
    const dispatcher = makeDispatcher({
      commands: [{ id: 'demo.run', title: '运行演示' }],
    })
    const transport = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(transport)
    render(<ExtensionHost dispatcher={dispatcher} />)

    pressPaletteShortcut()
    fireEvent.click(screen.getByText('运行演示'))

    expect(screen.queryByTestId('command-palette')).toBeNull()
    await waitFor(() => {
      expect(transport).toHaveBeenCalledWith('demo.run', undefined)
    })
  })

  it('卸载后快捷键不再打开面板（全局监听已清理）', () => {
    const dispatcher = makeDispatcher({ commands: [{ id: 'x', title: 'X' }] })
    const { unmount } = render(<ExtensionHost dispatcher={dispatcher} />)
    pressPaletteShortcut()
    expect(screen.getByTestId('command-palette')).toBeInTheDocument()
    unmount()

    pressPaletteShortcut()
    expect(screen.queryByTestId('command-palette')).toBeNull()
  })
})

describe('ExtensionHost — shell.overlay 槽锚点（modal 收敛）', () => {
  async function loadOverlay(entries: Array<Record<string, unknown>>): Promise<void> {
    await slotRegistry.loadFromSchema(
      { plugin_contributes: [{ plugin_id: 'demo', contributes: { slots: entries } }] },
      { loadWidgets: vi.fn().mockResolvedValue([]) },
    )
  }

  it('命令触发 shell.overlay 条目：声明 widget 渲染并透传 props', async () => {
    widgetRegistry.register('demo_widget', ((props: Record<string, unknown>) => (
      <span data-testid="demo-widget-content">参数 {String(props.level)}</span>
    )) as never, { name: 'demo_widget' })
    const dispatcher = makeDispatcher({ commands: [{ id: 'demo.open', title: '打开弹窗' }] })
    await loadOverlay([
      {
        id: 'demo.modal',
        slot: 'shell.overlay',
        openOn: 'on_command:demo.open',
        title: '演示弹窗',
        channel: 'component',
        widget: 'demo_widget',
        props: { level: 3 },
      },
    ])
    render(<ExtensionHost dispatcher={dispatcher} />)

    pressPaletteShortcut()
    fireEvent.click(screen.getByText('打开弹窗'))

    const overlay = await screen.findByTestId('slot-overlay')
    expect(overlay).toBeInTheDocument()
    expect(screen.getByText('演示弹窗')).toBeInTheDocument()
    expect(screen.getByTestId('demo-widget-content')).toHaveTextContent('参数 3')
  })

  it('渲染期 widget 未注册（装载后被禁用竞态）→ 可见占位不空白', async () => {
    widgetRegistry.register('ghost_widget', (() => <span>临时的</span>) as never, { name: 'ghost_widget' })
    const dispatcher = makeDispatcher({ commands: [{ id: 'demo.open', title: '打开弹窗' }] })
    await loadOverlay([
      {
        id: 'demo.modal',
        slot: 'shell.overlay',
        openOn: 'on_command:demo.open',
        title: '缺渲染器',
        channel: 'component',
        widget: 'ghost_widget',
      },
    ])
    // 装载后 widget 被注销（热禁用竞态窗口）
    widgetRegistry.unregister('ghost_widget')
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<ExtensionHost dispatcher={dispatcher} />)

    pressPaletteShortcut()
    fireEvent.click(screen.getByText('打开弹窗'))
    expect(await screen.findByTestId('slot-entry-error')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('标题栏关闭按钮与遮罩点击都能关闭', async () => {
    widgetRegistry.register('noop', (() => <span>内容</span>) as never, { name: 'noop' })
    const dispatcher = makeDispatcher({
      commands: [
        { id: 'demo.open', title: '打开弹窗' },
        { id: 'demo.open2', title: '再开一次' },
      ],
    })
    await loadOverlay([
      {
        id: 'demo.modal',
        slot: 'shell.overlay',
        openOn: 'on_command:demo.open',
        title: '多关闭路径',
        channel: 'component',
        widget: 'noop',
      },
      {
        id: 'demo.modal2',
        slot: 'shell.overlay',
        openOn: 'on_command:demo.open2',
        title: '多关闭路径',
        channel: 'component',
        widget: 'noop',
      },
    ])
    render(<ExtensionHost dispatcher={dispatcher} />)

    pressPaletteShortcut()
    fireEvent.click(screen.getByText('打开弹窗'))
    fireEvent.click(await screen.findByRole('button', { name: '关闭' }))
    await waitFor(() => {
      expect(screen.queryByTestId('slot-overlay')).toBeNull()
    })

    pressPaletteShortcut()
    fireEvent.click(screen.getByText('再开一次'))
    const reopened = await screen.findByTestId('slot-overlay')
    fireEvent.click(reopened)
    await waitFor(() => {
      expect(screen.queryByTestId('slot-overlay')).toBeNull()
    })
  })

  it('默认 dispatcher（全局单例）可用：不传 dispatcher 时快捷键打开面板', () => {
    const transport = vi.fn().mockResolvedValue(undefined)
    commandDispatcher.setTransport(transport)
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'ext_demo', contributes: { commands: [{ id: 'g.run', title: '全局命令' }] } },
      ],
    } as never)

    render(<ExtensionHost />)
    pressPaletteShortcut()
    expect(screen.getByText('全局命令')).toBeInTheDocument()
  })
})
