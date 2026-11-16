/** @feature FP-T12 前端槽位扩展机制 | @ci: frontend-test */
/**
 * SlotOverlayHost 测试（shell.overlay 锚点，contributes.modal 收敛呈现）
 *
 * 契约：
 * - 命令触发（contributes.commands + commandDispatcher 触发面不动）→
 *   slotRegistry.findOverlayByTrigger 命中 shell.overlay 条目 → 模态呈现
 * - 内容 = 条目声明的 widget（component 通道，props 下发）
 * - 关闭按钮/遮罩点击关闭；无触发零渲染
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { commandDispatcher, CommandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import { SlotOverlayHost } from '../SlotOverlayHost'

function PickWidget(): React.ReactElement {
  return <div data-testid="overlay-pick-widget">已选</div>
}

beforeEach(() => {
  widgetRegistry.clear()
  widgetRegistry.register('form', PickWidget, { name: 'form' })
  slotRegistry.clear()
  contributionRegistry.clear()
})

async function prepare(): Promise<CommandDispatcher> {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'plug_a',
        contributes: {
          commands: [{ id: 'plug_a.pick', title: '选择', category: 'demo' }],
          modal: [{ id: 'pick_thing', title: '选择', openOn: 'on_command:plug_a.pick', widget: 'form' }],
        },
      },
    ],
  } as never)
  await slotRegistry.loadFromSchema(
    {
      plugin_contributes: [
        {
          plugin_id: 'plug_a',
          contributes: {
            modal: [{ id: 'pick_thing', title: '选择', openOn: 'on_command:plug_a.pick', widget: 'form' }],
          },
        },
      ],
    },
    { loadWidgets: vi.fn().mockResolvedValue([]) },
  )
  const dispatcher = new CommandDispatcher(contributionRegistry)
  dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
  return dispatcher
}

describe('SlotOverlayHost（shell.overlay）', () => {
  it('命令触发 → shell.overlay 条目模态呈现（modal 收敛，触发面 commands 不动）', async () => {
    const dispatcher = await prepare()
    render(<SlotOverlayHost dispatcher={dispatcher} />)
    expect(screen.queryByTestId('slot-overlay')).toBeNull()
    await dispatcher.executeCommand('plug_a.pick')
    await waitFor(() => {
      expect(screen.getByTestId('slot-overlay')).toBeInTheDocument()
    })
    expect(screen.getByTestId('overlay-pick-widget')).toBeInTheDocument()
    expect(screen.getByText('选择')).toBeInTheDocument()
  })

  it('关闭按钮关闭模态', async () => {
    const dispatcher = await prepare()
    render(<SlotOverlayHost dispatcher={dispatcher} />)
    await dispatcher.executeCommand('plug_a.pick')
    await waitFor(() => {
      expect(screen.getByTestId('slot-overlay')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId('slot-overlay-close'))
    expect(screen.queryByTestId('slot-overlay')).toBeNull()
  })

  it('全局 commandDispatcher 单例同样可触发（无注入 dispatcher 的默认形态）', async () => {
    await prepare()
    commandDispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
    render(<SlotOverlayHost />)
    await commandDispatcher.executeCommand('plug_a.pick')
    await waitFor(() => {
      expect(screen.getByTestId('slot-overlay')).toBeInTheDocument()
    })
  })
})
