/** @feature FP-0.2.九 会话工作空间与会话列表（自定义分组） @vision V6 可即用 @ci frontend-test */
/**
 * SessionList 自定义分组契约测试
 *
 * 语义（用户裁定 2026-10-18）：
 * - 行菜单「分组…」打开分组对话框：可选已有组，也可填新组名建组并移入；
 * - 已分配会话从工作空间树/平铺列表收进自定义分组区块（单归属，一处出现）；
 * - 「移出分组」回落平铺列表；组头「删除分组」只拆组、成员回落；
 * - 区块头可折叠；挂靠工作空间的会话入自定义组后不再出现在工作空间树。
 */

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { listSessionGroups } from '@/services/sessionGroups'
import { uiStorage } from '@/utils/storage'
import { SessionList } from '../SessionList'
import { createSessionListCallbacks, makeSession, openDropdownMenu } from './sessionTestUtils'
import type { Session } from '@/types/models'

const callbacks = createSessionListCallbacks()

function renderList(sessions: Session[]) {
  return render(
    <SessionList
      sessions={sessions}
      activeSessionId={null}
      deletingSessionIds={new Set()}
      {...callbacks}
    />,
  )
}

/** 行菜单 → 「分组…」→ 对话框就绪 */
async function openGroupDialog(sessionTitle: string) {
  const row = screen.getByRole('button', { name: `会话: ${sessionTitle}` })
  await act(async () => {
    openDropdownMenu(within(row).getByRole('button', { name: '更多操作' }))
  })
  fireEvent.click(await screen.findByText('分组…'))
  return screen.findByRole('dialog')
}

beforeEach(() => {
  uiStorage.setSessionCustomGroups([])
})

afterEach(() => {
  cleanup()
  uiStorage.setSessionCustomGroups([])
})

describe('SessionList 自定义分组 — 建组与归属', () => {
  it('菜单「分组…」→ 填新组名保存：会话进自定义区块，离开平铺列表', async () => {
    const s = makeSession({ title: '目标会话' })
    renderList([s, makeSession({ title: '旁人会话' })])

    const dialog = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog).getByTestId('custom-group-save'))

    const header = await screen.findByTestId('custom-group-header')
    expect(header.textContent).toContain('工作')
    expect(header.textContent).toContain('(1)')
    // 组成员可见；同会话不再出现在平铺区
    expect(screen.getByRole('button', { name: '会话: 目标会话' })).toBeInTheDocument()
    // 服务侧落盘
    expect(listSessionGroups()).toHaveLength(1)
    expect(listSessionGroups()[0].sessionIds).toHaveLength(1)
  })

  it('已有组出现在对话框单选列表，选中保存即移入', async () => {
    // 复用同一会话对象：makeSession 每次生成新 id，重建会复现悬挂归属
    const first = makeSession({ title: '目标会话' })
    renderList([first])

    // 先建一组（走同一对话框链路），再用第二个会话复现「选已有组」
    const dialog1 = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog1).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog1).getByTestId('custom-group-save'))
    await screen.findByTestId('custom-group-header')

    const second = makeSession({ title: '第二会话' })
    cleanup()
    renderList([first, second])
    const dialog2 = await openGroupDialog('第二会话')
    const option = within(dialog2).getByTestId('custom-group-option')
    fireEvent.click(option)
    expect(option.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(within(dialog2).getByTestId('custom-group-save'))

    const header = await screen.findByTestId('custom-group-header')
    expect(header.textContent).toContain('(2)')
  })

  it('已归属会话的对话框预选当前组，可「移出分组」回落平铺', async () => {
    const s = makeSession({ title: '目标会话' })
    renderList([s])

    const dialog1 = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog1).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog1).getByTestId('custom-group-save'))
    await screen.findByTestId('custom-group-header')

    const dialog2 = await openGroupDialog('目标会话')
    // 预选：保存按钮可点且「移出分组」可见（来自预选态）
    expect(within(dialog2).getByTestId('custom-group-remove')).toBeInTheDocument()
    fireEvent.click(within(dialog2).getByTestId('custom-group-remove'))

    await screen.findByTestId('custom-group-header')
    // 组头仍在（空组不自动删），成员数为 0
    expect(screen.getByTestId('custom-group-header').textContent).toContain('(0)')
  })
})

describe('SessionList 自定义分组 — 区块结构与单归属', () => {
  it('删组只拆组：成员会话回落平铺列表，组头消失', async () => {
    const s = makeSession({ title: '目标会话' })
    renderList([s])

    const dialog = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog).getByTestId('custom-group-save'))
    await screen.findByTestId('custom-group-header')

    fireEvent.click(screen.getByTestId('custom-group-delete'))

    expect(screen.queryByTestId('custom-group-header')).toBeNull()
    expect(screen.getByRole('button', { name: '会话: 目标会话' })).toBeInTheDocument()
    // 会话未被删除（回调零调用）
    expect(callbacks.onDeleteSession).not.toHaveBeenCalled()
  })

  it('自定义组优先于工作空间树：挂靠目录的会话入组后不再出现在树里', async () => {
    const s = makeSession({ title: '项目会话', workspace: 'D:/proj/demo' })
    renderList([s])

    // 入组前：工作空间树含它
    expect(document.querySelector('[data-group="workspace-tree"]')).not.toBeNull()

    const dialog = await openGroupDialog('项目会话')
    fireEvent.change(within(dialog).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog).getByTestId('custom-group-save'))
    await screen.findByTestId('custom-group-header')

    // 入组后：树消失（该会话是唯一树成员），自定义区块持有它
    expect(document.querySelector('[data-group="workspace-tree"]')).toBeNull()
    const section = document.querySelector(
      '[data-group="custom-tree"]',
    ) as HTMLElement
    expect(
      within(section).getByRole('button', { name: '会话: 项目会话' }),
    ).toBeInTheDocument()
  })

  it('区块头可折叠/展开', async () => {
    const s = makeSession({ title: '目标会话' })
    renderList([s])

    const dialog = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog).getByTestId('custom-group-save'))
    await screen.findByTestId('custom-group-header')

    const toggle = screen.getByTestId('custom-group-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: '会话: 目标会话' })).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: '会话: 目标会话' })).toBeInTheDocument()
  })

  it('空组也渲染区块头（计数 0），不隐藏用户刚建的空目录', async () => {
    renderList([makeSession({ title: '目标会话' })])

    const dialog = await openGroupDialog('目标会话')
    fireEvent.change(within(dialog).getByTestId('custom-group-name-input'), {
      target: { value: '工作' },
    })
    fireEvent.click(within(dialog).getByTestId('custom-group-save'))
    const header = await screen.findByTestId('custom-group-header')
    expect(header.textContent).toContain('(1)')

    // 移出后组头仍在，计数 0
    const dialog2 = await openGroupDialog('目标会话')
    fireEvent.click(within(dialog2).getByTestId('custom-group-remove'))
    await screen.findByText('(0)')
    expect(screen.getByTestId('custom-group-header').textContent).toContain('(0)')
  })
})
