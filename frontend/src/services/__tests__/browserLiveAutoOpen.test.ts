/** @feature FP-0.2.二 browser-live 自动打开 | @ci: frontend-test */
/**
 * browserLiveAutoOpen 测试 — tool_start 识别面
 *
 * - browser_ 前缀六件工具 → openDeclaredPluginPage({browser_tool, browser_live})
 * - 非 browser 工具 / 空名 → 零动作
 */
import { describe, expect, it, vi, beforeEach, type Mock } from 'vitest'

const openDeclaredPluginPage = vi.fn()
vi.mock('@/services/workspacePanelOpener', () => ({
  openDeclaredPluginPage: (...args: unknown[]) => openDeclaredPluginPage(...args),
}))

import { autoOpenBrowserLiveOnToolStart } from '@/services/browserLiveAutoOpen'

beforeEach(() => {
  openDeclaredPluginPage.mockClear()
  openDeclaredPluginPage.mockReturnValue(true)
})

describe('autoOpenBrowserLiveOnToolStart', () => {
  it.each([
    'browser_navigate',
    'browser_snapshot',
    'browser_click',
    'browser_type',
    'browser_take_screenshot',
    'browser_console_messages',
  ])('%s → 打开 browser_tool.browser_live', (tool) => {
    expect(autoOpenBrowserLiveOnToolStart(tool)).toBe(true)
    expect(openDeclaredPluginPage).toHaveBeenCalledWith({
      pluginId: 'browser_tool',
      pageId: 'browser_live',
    })
  })

  it('非浏览器工具零动作', () => {
    expect(autoOpenBrowserLiveOnToolStart('bash_execute')).toBe(false)
    expect(autoOpenBrowserLiveOnToolStart('web_operate')).toBe(false)
    expect(openDeclaredPluginPage).not.toHaveBeenCalled()
  })

  it('空名/缺失名零动作', () => {
    expect(autoOpenBrowserLiveOnToolStart(undefined)).toBe(false)
    expect(autoOpenBrowserLiveOnToolStart('')).toBe(false)
    expect(openDeclaredPluginPage).not.toHaveBeenCalled()
  })
})
