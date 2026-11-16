/**
 * ExtensionComponents 测试（P5-a/b 渲染层）
 *
 * - CommandPalette：搜索 + 执行 + when 过滤
 * - ContextMenu：按 location + when 渲染菜单项
 *
 * 模态弹窗呈现已收敛至 shell.overlay 槽锚点（SlotOverlayHost，C4 槽位机制），
 * 通道/触发覆盖见 components/slots/__tests__/SlotOverlayHost.test.tsx。
 *
 * 真实渲染被测组件，仅 Mock transport（CommandDispatcher 注入）。
 */

import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { CommandPalette, ContextMenuItems } from '@/components/extension/ExtensionComponents'
import { CommandDispatcher } from '@/services/schema/commandDispatcher'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'

function setupRegistry(contributes: Record<string, unknown[]>): ContributionRegistry {
  const registry = new ContributionRegistry()
  registry.loadFromSchema({
    plugin_contributes: [{ plugin_id: 'ext', contributes }],
  } as never)
  return registry
}

describe('CommandPalette — 命令面板（P5-b）', () => {
  let registry: ContributionRegistry
  let dispatcher: CommandDispatcher

  beforeEach(() => {
    useContextKeys.getState().reset()
    registry = setupRegistry({
      commands: [
        { id: 'ext.showReport', title: '显示报告', category: '报告' },
        { id: 'ext.export', title: '导出', category: '报告' },
        { id: 'ext.runOnly', title: '运行专属', when: 'pipeline.running' },
      ],
    })
    dispatcher = new CommandDispatcher(registry)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
  })

  it('打开时显示所有可见命令（when 过滤）', () => {
    render(<CommandPalette open={true} dispatcher={dispatcher} onClose={() => {}} />)
    // 默认 pipeline 未运行 → runOnly 被过滤
    expect(screen.getByText('显示报告')).toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
    expect(screen.queryByText('运行专属')).not.toBeInTheDocument()
  })

  it('搜索框过滤命令', () => {
    render(<CommandPalette open={true} dispatcher={dispatcher} onClose={() => {}} />)
    const input = screen.getByPlaceholderText(/搜索命令/)
    fireEvent.change(input, { target: { value: '导出' } })
    expect(screen.queryByText('显示报告')).not.toBeInTheDocument()
    expect(screen.getByText('导出')).toBeInTheDocument()
  })

  it('点击命令项触发执行并关闭', () => {
    const onClose = vi.fn()
    render(<CommandPalette open={true} dispatcher={dispatcher} onClose={onClose} />)
    fireEvent.click(screen.getByText('显示报告'))
    expect(dispatcher['transport']).toHaveBeenCalledWith('ext.showReport', undefined)
    expect(onClose).toHaveBeenCalled()
  })

  it('open=false 时不渲染', () => {
    render(<CommandPalette open={false} dispatcher={dispatcher} onClose={() => {}} />)
    expect(screen.queryByPlaceholderText(/搜索命令/)).not.toBeInTheDocument()
  })
})

describe('ContextMenuItems — 右键菜单（P5-a）', () => {
  let registry: ContributionRegistry
  let dispatcher: CommandDispatcher

  beforeEach(() => {
    useContextKeys.getState().reset()
    registry = setupRegistry({
      menus: [
        { id: 'ext.goto', location: 'workspace/context', title: '跳转', command: 'ext.goto' },
        { id: 'ext.pyOnly', location: 'workspace/context', title: 'Python 专属', command: 'ext.py', when: "resource.extname == '.py'" },
        { id: 'ext.chat', location: 'chat/context', title: '聊天菜单', command: 'ext.chatCmd' },
      ],
    })
    dispatcher = new CommandDispatcher(registry)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
  })

  it('按 location 过滤 + when 过滤渲染菜单项', () => {
    const { rerender } = render(
      <ContextMenuItems location="workspace/context" dispatcher={dispatcher} />,
    )
    expect(screen.getByText('跳转')).toBeInTheDocument()
    // pyOnly 默认失配（resource.extname 非空才命中，默认空字符串）
    expect(screen.queryByText('Python 专属')).not.toBeInTheDocument()
    // chat/context 项不在 workspace/context
    expect(screen.queryByText('聊天菜单')).not.toBeInTheDocument()

    // 切到 Python 文件后 pyOnly 可见
    useContextKeys.getState().setResource({ isFile: true, extname: '.py' })
    rerender(<ContextMenuItems location="workspace/context" dispatcher={dispatcher} />)
    expect(screen.getByText('Python 专属')).toBeInTheDocument()
  })

  it('点击菜单项触发 command', () => {
    render(<ContextMenuItems location="workspace/context" dispatcher={dispatcher} />)
    fireEvent.click(screen.getByText('跳转'))
    expect(dispatcher['transport']).toHaveBeenCalledWith('ext.goto', undefined)
  })

  it('无可见项时渲染空提示', () => {
    render(<ContextMenuItems location="nonexistent/context" dispatcher={dispatcher} />)
    // 无菜单项时不渲染任何菜单按钮
    expect(screen.queryByText('跳转')).not.toBeInTheDocument()
  })
})
