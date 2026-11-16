/** @feature FP-0.2.移动端适配 视图切换链路 | @ci frontend-test */
/**
 * 面板切换链路锁定（真机 C5 探针场景：多面板标签已开 + 某文件标签激活）。
 *
 * 契约：侧栏导航点击的处理链（openWorkspacePanelByPath → 幂等激活既有页签 /
 * 新开并激活）在"多标签已开 + 文件标签激活"状态下仍正确翻转激活态——目标签
 * aria-selected 翻真、内容区摘 hidden、原激活文件签让位。该链路若断，侧栏
 * 点击的表现即"点了没反应"。注意本文件不 mock workspacePanelOpener（被测
 * 对象就是真实 opener → layoutModeStore → WorkspacePanel 全链）。
 */

import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { openWorkspacePanel, openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { ResizeObserverStub, setViewportWidth } from './helpers/fiveSpaceTestUtils'

vi.mock('@/components/workspace/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="mock-code-editor" />,
}))

function seedTabs() {
  useSessionStore.setState({ activeSessionId: 'sess-1' })
  useLayoutModeStore.setState({
    workspaceTabs: [
      {
        id: 'ws-panel-tasks',
        title: '任务管理',
        icon: 'folder',
        moduleId: '__panel_tasks__',
        component: 'pipeline_manager',
        isActive: false,
        isPinned: false,
      },
      {
        id: 'ws-plugin-monitor',
        title: '监控',
        icon: 'activity',
        moduleId: '__plugin_monitoring__',
        component: 'monitoring_hub',
        isActive: false,
        isPinned: false,
      },
      {
        // 文件标签：归属当前会话（文件树派生链），处于激活态——C5 场景核心前置
        id: 'file-ctask-D__proj_report_md',
        title: 'report.md',
        icon: '📄',
        moduleId: '__file_editor__',
        ownerSessionId: 'sess-1',
        isActive: true,
        isPinned: false,
      },
    ],
    // 全部已访问：内容区都真实挂载，激活翻转表现为 hidden ↔ h-full
    visitedTabIds: ['ws-panel-tasks', 'ws-plugin-monitor', 'file-ctask-D__proj_report_md'],
  })
  useUIStore.setState({ sidebarCollapsed: false, workspaceCollapsed: false })
}

/** 内容区让位断言：页签标题在页签条与内容兜底各出现一次，内容侧才带 aria-hidden 包装 */
function contentAriaHiddenOf(title: string): string | null {
  const candidates = screen.getAllByText(title)
  for (const node of candidates) {
    const scope = node.closest('div[aria-hidden]')
    if (scope) return scope.getAttribute('aria-hidden')
  }
  return null
}

describe('面板切换链路（多标签已开 + 文件签激活）', () => {
  beforeEach(() => {
    setViewportWidth(1280)
    window.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
    seedTabs()
  })

  it('侧栏导航链（openWorkspacePanelByPath）幂等激活既有任务签：激活态与内容让位翻转', () => {
    render(
      <MemoryRouter>
        <FiveSpaceLayout
          chatContent={<div>对话内容</div>}
          sidebarContent={<div>侧栏导航</div>}
        />
      </MemoryRouter>,
    )

    // 前置：文件签激活，任务签未激活
    expect(screen.getByTestId('workspace-tab-file-ctask-D__proj_report_md')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByTestId('workspace-tab-ws-panel-tasks')).toHaveAttribute(
      'aria-selected',
      'false',
    )

    // 侧栏「任务管理」点击的处理链（handleTasksClick 同款调用）
    act(() => {
      expect(openWorkspacePanelByPath('/tasks')).toBe(true)
    })

    // 激活态翻转：任务签选中、文件签让位
    expect(screen.getByTestId('workspace-tab-ws-panel-tasks')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByTestId('workspace-tab-file-ctask-D__proj_report_md')).toHaveAttribute(
      'aria-selected',
      'false',
    )
    // 内容区同步：任务签内容可见，文件签内容（无编辑器数据兜底文案）hidden 让位
    expect(contentAriaHiddenOf('任务管理')).toBe('false')
    expect(contentAriaHiddenOf('文件数据已过期')).toBe('true')
  })

  it('连开两个面板（任务 → 监控）：激活态逐次跟随最后打开的面板', () => {
    render(
      <MemoryRouter>
        <FiveSpaceLayout
          chatContent={<div>对话内容</div>}
          sidebarContent={<div>侧栏导航</div>}
        />
      </MemoryRouter>,
    )

    act(() => {
      openWorkspacePanelByPath('/tasks')
    })
    // 监控是插件声明页（真实环境经 contributionRegistry 解析 path），测试注册表
    // 为空——与 handlePluginClick 的 widget 分支同款直开（幂等激活既有签）
    act(() => {
      openWorkspacePanel({
        id: 'ws-plugin-monitor',
        title: '监控',
        component: 'monitoring_hub',
        icon: 'activity',
        moduleId: '__plugin_monitoring__',
      })
    })

    expect(screen.getByTestId('workspace-tab-ws-plugin-monitor')).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByTestId('workspace-tab-ws-panel-tasks')).toHaveAttribute(
      'aria-selected',
      'false',
    )
    expect(contentAriaHiddenOf('监控')).toBe('false')
  })
})
