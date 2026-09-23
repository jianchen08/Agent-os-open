// @feature: FP-T12 前端适配 | @ci: frontend-test
/**
 * SessionList 置顶行动作作用域回归锁（BUG-78）
 *
 * 立案缺陷：置顶序列下会话「编辑改名」+「删除」被疑与后端脱钩（操作作用于
 * 错误行对象）。本文件锁定行作用域契约：置顶分组与普通分组共存时，每行的
 * 菜单文案与动作回调必须绑定该行自己的会话对象/id，不得串行。
 *
 * Dialog 以受控 mock 渲染（Radix Portal 在 jsdom 下不稳定），使删除确认层
 * 的目标行标题可确定性断言。
 */

import { cleanup, fireEvent, render, screen, waitFor, within, act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SessionList } from '../SessionList'
import { createSessionListCallbacks, makeSession, openDropdownMenu } from './sessionTestUtils'

/** Dialog 以受控 mock 渲染（Radix Portal 在 jsdom 下不稳定）；透传壳组件在
 *  工厂内局部定义（工厂闭包不可引用模块级绑定）。 */
vi.mock('@/components/ui/dialog', () => {
  const Shell = ({ children }: any) => <div>{children}</div>
  return {
    Dialog: ({ open, children }: any) => (open ? <div role="dialog">{children}</div> : null),
    DialogContent: Shell,
    DialogDescription: Shell,
    DialogFooter: Shell,
    DialogHeader: Shell,
    DialogTitle: Shell,
  }
})

const callbacks = createSessionListCallbacks()

/** 渲染「置顶A + 普通B」两行共存的列表，返回行定位器 */
function renderPinnedAndNormal() {
  const pinned = makeSession({ id: 'pinned-A', title: '置顶会话A', pinned: true })
  const normal = makeSession({ id: 'normal-B', title: '普通会话B', pinned: false })
  render(
    <SessionList
      sessions={[pinned, normal]}
      activeSessionId={null}
      deletingSessionIds={new Set()}
      {...callbacks}
    />,
  )
  const rowOf = (title: string) => screen.getByRole('button', { name: `会话: ${title}` })
  return { pinned, normal, rowOf }
}

function openRowMenu(row: HTMLElement): void {
  openDropdownMenu(within(row).getByRole('button', { name: /更多操作/ }))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BUG-78 回归锁：置顶行动作作用域', () => {
  it('置顶行菜单文案为「取消置顶」（随该行状态，非全局文案）', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('置顶会话A'))
    })
    expect(await screen.findByText('取消置顶')).toBeInTheDocument()
  })

  it('普通行菜单文案为「置顶会话」（同行置顶行共存时仍随各自状态）', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('普通会话B'))
    })
    expect(await screen.findByText('置顶会话')).toBeInTheDocument()
  })

  it('点击置顶行「取消置顶」以该行会话 id 回调', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('置顶会话A'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('取消置顶'))
    })
    expect(callbacks.onPinSession).toHaveBeenCalledTimes(1)
    expect(callbacks.onPinSession).toHaveBeenCalledWith('pinned-A')
  })

  it('点击普通行「置顶会话」以该行会话 id 回调（不串到置顶行）', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('普通会话B'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('置顶会话'))
    })
    expect(callbacks.onPinSession).toHaveBeenCalledTimes(1)
    expect(callbacks.onPinSession).toHaveBeenCalledWith('normal-B')
  })

  it('置顶行「编辑会话」携带该行会话对象（改名作用于正确对象）', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('置顶会话A'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('编辑会话'))
    })
    expect(callbacks.onEditSession).toHaveBeenCalledTimes(1)
    expect(callbacks.onEditSession).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'pinned-A', title: '置顶会话A' }),
    )
  })

  it('置顶行删除确认弹窗标题随目标行，确认后以该行 id 删除', async () => {
    const { rowOf } = renderPinnedAndNormal()
    await act(async () => {
      openRowMenu(rowOf('置顶会话A'))
    })
    await act(async () => {
      fireEvent.click(await screen.findByText('删除'))
    })

    const dialog = await waitFor(() => {
      const el = screen.getByRole('dialog')
      expect(el.textContent).toContain('确认删除')
      expect(el.textContent).toContain('置顶会话A')
      return el
    })
    expect(dialog.textContent).not.toContain('普通会话B')

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: /确认删除/ }))
    })
    await waitFor(() => {
      expect(callbacks.onDeleteSession).toHaveBeenCalledTimes(1)
    })
    expect(callbacks.onDeleteSession).toHaveBeenCalledWith('pinned-A')
  })
})
