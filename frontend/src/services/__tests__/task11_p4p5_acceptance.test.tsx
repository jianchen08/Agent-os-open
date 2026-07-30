/**
 * task_11 P4/P5 验收测试（ADR §3.4 + §5.5 + §5.7）
 *
 * 验证：插件在 manifest.contributes 声明后，前端自动出现入口
 * （menu/command/shortcut/modal 各一）+ when 求值正确 + TopNav 删除。
 *
 * 模拟一个测试插件声明 contributes，加载到 ContributionRegistry 后：
 * - activity bar 入口（viewsContainers）+ 侧边栏视图（views）+ workspaceTab 自动出现
 * - menu/command/shortcut/modal 各一项可查询
 * - when 条件命中/失配可见性正确
 *
 * 不改前端代码即出现入口——内核聚合已就绪（6f242bc2），前端只读 schema 渲染。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import {
  CommandPalette,
  ContextMenuItems,
  ExtensionModalHost,
  useExtensionModal,
} from '@/components/extension/ExtensionComponents'
import { CommandDispatcher } from '@/services/schema/commandDispatcher'
import { ContributionRegistry, contributionRegistry } from '@/services/schema/ContributionRegistry'
import { ShortcutRegistry } from '@/services/schema/shortcutRegistry'
import { evaluateWhen } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'
import React from 'react'

/**
 * 测试插件 manifest（模拟内核聚合后的 schema 输出）
 *
 * 一个插件同时声明 menu/command/shortcut/modal + workspaceTab + viewsContainer
 */
const TEST_PLUGIN_SCHEMA = {
  plugin_contributes: [
    {
      plugin_id: 'demo-ext',
      name: '演示扩展',
      contributes: {
        // P5-a 菜单
        menus: [
          {
            id: 'demo.copyPath',
            location: 'workspace/context',
            title: '复制路径',
            command: 'demo.copyPath',
            when: 'resource.isFile',
          },
        ],
        // P5-b 命令
        commands: [
          { id: 'demo.greet', title: '打招呼', category: '演示' },
        ],
        // P5-c 快捷键
        shortcuts: [
          { command: 'demo.greet', key: 'Ctrl+G', when: 'workspace.focus' },
        ],
        // P5-d 模态弹窗
        modal: [
          {
            id: 'demo.greetingModal',
            title: '问候',
            trigger: 'on_command:demo.greet',
            widget: 'greeting',
            props: { msg: '你好' },
          },
        ],
        // P4-d 工作区 tab + 视图容器（导航入口）
        workspaceTabs: [{ id: 'demo.tab', title: '演示面板', widget: 'demo_view' }],
        viewsContainers: [{ id: 'demo', title: '演示', icon: 'star', path: '/demo' }],
        views: [{ containerId: 'demo', id: 'demo.list', name: '演示列表', widget: 'list' }],
      },
    },
  ],
}

describe('task_11 P4/P5 验收：插件声明 contributes 后前端自动出现入口', () => {
  let registry: ContributionRegistry
  let dispatcher: CommandDispatcher
  let shortcuts: ShortcutRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
    registry.loadFromSchema(TEST_PLUGIN_SCHEMA as never)
    dispatcher = new CommandDispatcher(registry)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
    shortcuts = new ShortcutRegistry(registry)
    shortcuts.refresh()
    useContextKeys.getState().reset()
  })

  describe('P4-d 自动入口（不改前端代码）', () => {
    it('contributes.viewsContainers 自动出现导航入口', () => {
      const containers = registry.getViewsContainers()
      expect(containers).toHaveLength(1)
      expect(containers[0].id).toBe('demo')
      expect(containers[0].title).toBe('演示')
    })

    it('contributes.views 自动出现侧边栏视图', () => {
      const views = registry.getViews('demo')
      expect(views).toHaveLength(1)
      expect(views[0].id).toBe('demo.list')
    })

    it('contributes.workspaceTabs 自动出现工作区 tab', () => {
      const tabs = registry.getWorkspaceTabs()
      expect(tabs).toHaveLength(1)
      expect(tabs[0].id).toBe('demo.tab')
    })
  })

  describe('P5-a menu（右键菜单）', () => {
    it('contributes.menus 可查询，按 location + when 过滤', () => {
      // 默认 resource.isFile=false → 失配
      let visible = dispatcher.getVisibleMenus('workspace/context')
      expect(visible).toEqual([])

      // 切到文件后命中
      useContextKeys.getState().setResource({ isFile: true, extname: '.txt' })
      visible = dispatcher.getVisibleMenus('workspace/context')
      expect(visible).toHaveLength(1)
      expect(visible[0].title).toBe('复制路径')
    })

    it('渲染层 ContextMenuItems 反映 when 过滤结果', () => {
      const { rerender } = render(
        <ContextMenuItems location="workspace/context" dispatcher={dispatcher} />,
      )
      expect(screen.queryByText('复制路径')).not.toBeInTheDocument()

      useContextKeys.getState().setResource({ isFile: true, extname: '.txt' })
      rerender(<ContextMenuItems location="workspace/context" dispatcher={dispatcher} />)
      expect(screen.getByText('复制路径')).toBeInTheDocument()
    })
  })

  describe('P5-b command（命令面板）', () => {
    it('contributes.commands 聚合到命令面板', () => {
      render(<CommandPalette open={true} dispatcher={dispatcher} onClose={() => {}} />)
      expect(screen.getByText('打招呼')).toBeInTheDocument()
    })

    it('搜索能命中插件命令', () => {
      render(<CommandPalette open={true} dispatcher={dispatcher} onClose={() => {}} />)
      fireEvent.change(screen.getByPlaceholderText(/搜索命令/), { target: { value: '招呼' } })
      expect(screen.getByText('打招呼')).toBeInTheDocument()
    })
  })

  describe('P5-c shortcut（快捷键）', () => {
    it('contributes.shortcuts 注册并可匹配', () => {
      const bindings = shortcuts.getBindings()
      expect(bindings).toHaveLength(1)
      expect(bindings[0].command).toBe('demo.greet')

      // 模拟 Ctrl+G
      const ev = { ctrlKey: true, shiftKey: false, altKey: false, metaKey: false, key: 'g' }
      expect(shortcuts.matchKey(ev as unknown as KeyboardEvent)).toBe('demo.greet')
    })

    it('when 失配时不触发', () => {
      // workspace.focus 默认 false
      expect(shortcuts.shouldFire('demo.greet')).toBe(false)
      useContextKeys.getState().setWorkspaceFocus(true)
      expect(shortcuts.shouldFire('demo.greet')).toBe(true)
    })
  })

  describe('P5-d modal（模态弹窗）', () => {
    it('contributes.modal 命令触发后弹出声明的 widget', async () => {
      function Host() {
        const { modal, closeModal } = useExtensionModal(dispatcher)
        if (!modal) return null
        return (
          <ExtensionModalHost modal={modal} onClose={closeModal}>
            <span>{`greeting-${String((modal.props as { msg?: string }).msg)}`}</span>
          </ExtensionModalHost>
        )
      }

      render(<Host />)
      expect(screen.queryByText('问候')).not.toBeInTheDocument()

      await act(async () => {
        await dispatcher.executeCommand('demo.greet')
      })
      await waitFor(() => expect(screen.getByText('问候')).toBeInTheDocument())
      expect(screen.getByText('greeting-你好')).toBeInTheDocument()
    })
  })

  describe('when 求值器核心正确性', () => {
    it('resource.extname == ".py" 仅 Python 命中', () => {
      const pyCtx = { 'resource.extname': '.py' }
      const jsCtx = { 'resource.extname': '.js' }
      expect(evaluateWhen('resource.extname == \'.py\'', pyCtx)).toBe(true)
      expect(evaluateWhen('resource.extname == \'.py\'', jsCtx)).toBe(false)
    })

    it('pipeline.running && workspace.focus 组合', () => {
      expect(evaluateWhen('pipeline.running && workspace.focus', {
        'pipeline.running': true,
        'workspace.focus': true,
      })).toBe(true)
      expect(evaluateWhen('pipeline.running && workspace.focus', {
        'pipeline.running': true,
        'workspace.focus': false,
      })).toBe(false)
    })

    it('!interaction.pending 取反', () => {
      expect(evaluateWhen('!interaction.pending', { 'interaction.pending': false })).toBe(true)
      expect(evaluateWhen('!interaction.pending', { 'interaction.pending': true })).toBe(false)
    })
  })

  describe('TopNav 死代码删除验证（P4-a）', () => {
    it('TopNav.tsx 文件已删除', async () => {
      // 直接检查文件系统，避免动态导入触发庞大依赖链
      const fs = await import('node:fs')
      const path = await import('node:path')
      const topNavPath = path.resolve(__dirname, '../../components/layout/TopNav.tsx')
      expect(fs.existsSync(topNavPath)).toBe(false)
    })

    it('layout/index.ts 不再导出 TopNav/NAV_ITEMS/isNavItemActive', async () => {
      const fs = await import('node:fs')
      const path = await import('node:path')
      const indexPath = path.resolve(__dirname, '../../components/layout/index.ts')
      const content = fs.readFileSync(indexPath, 'utf-8')
      expect(content).not.toContain('TopNav')
      expect(content).not.toContain('NAV_ITEMS')
      expect(content).not.toContain('isNavItemActive')
    })

    it('contributionRegistry 单例可用（基线修复后）', () => {
      expect(contributionRegistry).toBeInstanceOf(ContributionRegistry)
    })
  })
})
