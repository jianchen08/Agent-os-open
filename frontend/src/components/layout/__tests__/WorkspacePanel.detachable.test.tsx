/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * WorkspacePanel 页签 detachable 弹出入口（模式面板悬浮窗缺口修复）
 *
 * 契约（contributes.pages[].detachable 声明 → 工作区页签入口）：
 * - 声明了 detachable（popout 等）的页面页签 → 右键菜单出现「弹出为浮窗」，
 *   点击经 windowManager.openPopout 弹出（Web 版落 layoutModeStore.floatingWindows）
 * - 未声明 detachable / 声明 popout:false / 非声明页签（无 pageId）→ 不出现入口
 *   （popout:false 时 openPopout 为 no-op，不得给死入口）
 *
 * 测试策略：真实 ContributionRegistry / workspacePanelOpener / layoutModeStore /
 * windowManager（Web 实现落 store），无 mock，断言可观察副作用。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { openPluginPage } from '@/services/workspacePanelOpener'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { WorkspacePanel } from '../WorkspacePanel'

const DETACHABLE_PAGE: PageDeclaration = {
  type: 'pages',
  id: 'godot_dev',
  title: 'Godot 开发',
  icon: '🎮',
  space: 'workspace',
  slot: 'tab',
  path: '/p/godot_dev',
  widget: 'webview',
  props: { pluginId: 'mode_godot', htmlPath: '/page/godot-panel', widgetId: 'godot_dev' },
  pluginId: 'mode_godot',
  detachable: { popout: true, childWindow: true, defaultSize: { w: 400, h: 680 } },
}

const PLAIN_PAGE: PageDeclaration = {
  type: 'pages',
  id: 'plain_page',
  title: '普通页',
  space: 'workspace',
  slot: 'tab',
  widget: 'table',
  pluginId: 'mode_plain',
}

const POPOUT_DENIED_PAGE: PageDeclaration = {
  type: 'pages',
  id: 'denied_page',
  title: '禁弹页',
  space: 'workspace',
  slot: 'tab',
  widget: 'table',
  pluginId: 'mode_denied',
  detachable: { popout: false, childWindow: true },
}

function renderPanel() {
  return render(
    <WorkspacePanel
      tabs={useLayoutModeStore.getState().workspaceTabs}
      onTabChange={() => {}}
      onTabClose={() => {}}
      renderTabContent={() => <div data-testid="tab-content" />}
    />,
  )
}

function openTabMenu(testId: string) {
  fireEvent.contextMenu(screen.getByTestId(testId))
}

describe('WorkspacePanel 页签 detachable 弹出入口', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useLayoutModeStore.setState({ workspaceTabs: [], floatingWindows: [], visitedTabIds: [] })
  })

  it('声明 detachable 的页签 → 菜单出现「弹出为浮窗」，点击弹出浮窗（props.pageId 反查声明）', () => {
    contributionRegistry.register(DETACHABLE_PAGE)
    expect(openPluginPage(DETACHABLE_PAGE)).toBe(true)
    renderPanel()

    openTabMenu('workspace-tab-ws-plugin-godot_dev')
    const item = screen.getByTestId('workspace-tab-menu-popout')
    expect(item).toHaveTextContent('弹出为浮窗')

    fireEvent.click(item)
    const wins = useLayoutModeStore.getState().floatingWindows
    expect(wins).toHaveLength(1)
    expect(wins[0]?.props?.pageId).toBe('godot_dev')
    expect(wins[0]?.component).toBe('webview')
    // 弹出后菜单收起
    expect(screen.queryByTestId('workspace-tab-menu-popout')).not.toBeInTheDocument()
  })

  it('未声明 detachable 的页签 → 菜单无弹出项（其余菜单项不受影响）', () => {
    contributionRegistry.register(PLAIN_PAGE)
    expect(openPluginPage(PLAIN_PAGE)).toBe(true)
    renderPanel()

    openTabMenu('workspace-tab-ws-plugin-plain_page')
    expect(screen.queryByTestId('workspace-tab-menu-popout')).not.toBeInTheDocument()
    expect(screen.getByTestId('workspace-tab-menu-close')).toBeInTheDocument()
    expect(useLayoutModeStore.getState().floatingWindows).toHaveLength(0)
  })

  it('声明 popout:false 的页签 → 无弹出项（openPopout 为 no-op，不给死入口）', () => {
    contributionRegistry.register(POPOUT_DENIED_PAGE)
    expect(openPluginPage(POPOUT_DENIED_PAGE)).toBe(true)
    renderPanel()

    openTabMenu('workspace-tab-ws-plugin-denied_page')
    expect(screen.queryByTestId('workspace-tab-menu-popout')).not.toBeInTheDocument()
  })

  it('非声明页签（无 pageId，如文件编辑器页签）→ 无弹出项', () => {
    useLayoutModeStore.getState().addWorkspaceTab({
      id: 'file-x',
      title: 'main.py',
      moduleId: '__file_editor__',
      isActive: true,
      isPinned: false,
    })
    renderPanel()

    openTabMenu('workspace-tab-file-x')
    expect(screen.queryByTestId('workspace-tab-menu-popout')).not.toBeInTheDocument()
  })
})
