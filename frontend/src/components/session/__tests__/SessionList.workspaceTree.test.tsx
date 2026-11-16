// @feature: FP-0.2.九 会话树按工作空间聚合 | @ci: frontend-test
/**
 * SessionList 会话树（按工作空间聚合）契约测试
 *
 * 需求语义：
 * - 显式选择了工作空间的会话按目录聚合成会话树；树顶栏=空间封面
 *   （目录名 + 会话数），可折叠，并提供「以此空间新建会话」入口
 * - 未填工作空间（默认空间）的会话不聚合，留在「默认空间」平铺组
 * - 置顶会话只作视觉标记（R10）：不再独立浮顶分组，照常按工作空间入树
 *
 * 防拟合：尾分隔符同目录合并（性质：聚合键归一）、多空间组间按最新活动排序、
 * 组内 updatedAt 降序、回调收到组内最新会话的原始路径。
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SessionList } from '../SessionList'
import { createSessionListCallbacks, makeSession } from './sessionTestUtils'
import type { Session } from '@/types/models'

afterEach(cleanup)

function renderList(sessions: Session[], onCreateInWorkspace?: (workspace: string) => void) {
  const callbacks = createSessionListCallbacks()
  return render(
    <SessionList
      sessions={sessions}
      activeSessionId={null}
      deletingSessionIds={new Set()}
      {...callbacks}
      onCreateInWorkspace={onCreateInWorkspace}
    />,
  )
}

describe('SessionList 会话树 — 聚合语义', () => {
  it('显式工作空间的会话聚合成树，默认空间会话只留在平铺组', () => {
    const wsSession = makeSession({ title: '项目会话', workspace: 'D:/proj/demo' })
    const defaultSession = makeSession({ title: '闲聊会话' })

    renderList([wsSession, defaultSession])

    // 树内含项目会话；平铺组只含默认空间会话
    const tree = document.querySelector('[data-group="workspace-tree"]') as HTMLElement
    expect(tree).not.toBeNull()
    expect(within(tree).getByText('项目会话')).toBeInTheDocument()

    const normalGroup = document.querySelector('[data-group="normal"]') as HTMLElement
    expect(within(normalGroup).getByText('闲聊会话')).toBeInTheDocument()
    expect(within(normalGroup).queryByText('项目会话')).toBeNull()
  })

  it('尾分隔符差异同目录归一组（聚合键归一），顶栏目录名取路径末段', () => {
    const a = makeSession({ title: '会话A', workspace: 'D:/proj/demo', updatedAt: '2026-01-02T00:00:00Z' })
    const b = makeSession({ title: '会话B', workspace: 'D:/proj/demo/', updatedAt: '2026-01-01T00:00:00Z' })

    renderList([a, b])

    const trees = document.querySelectorAll('[data-group="workspace-tree"]')
    expect(trees).toHaveLength(1)
    expect(screen.getByTestId('workspace-tree-header').textContent).toContain('demo')
    expect(screen.getByTestId('workspace-tree-header').textContent).toContain('(2)')
  })

  it('组间按最新会话活动排序，组内 updatedAt 降序', () => {
    const olderGroupNew = makeSession({ id: 'o1', title: '旧空间-新', workspace: 'D:/ws/old', updatedAt: '2026-01-03T00:00:00Z' })
    const olderGroupOld = makeSession({ id: 'o2', title: '旧空间-旧', workspace: 'D:/ws/old', updatedAt: '2026-01-01T00:00:00Z' })
    const newerGroup = makeSession({ id: 'n1', title: '新空间', workspace: 'D:/ws/new', updatedAt: '2026-01-05T00:00:00Z' })

    renderList([olderGroupOld, olderGroupNew, newerGroup])

    const trees = document.querySelectorAll('[data-group="workspace-tree"]')
    expect(trees).toHaveLength(2)
    expect(trees[0].getAttribute('data-workspace-key')).toBe('D:/ws/new')
    expect(trees[1].getAttribute('data-workspace-key')).toBe('D:/ws/old')

    const oldGroupRows = within(trees[1] as HTMLElement).getAllByRole('button', { name: /^会话:/ })
    expect(oldGroupRows[0].textContent).toContain('旧空间-新')
    expect(oldGroupRows[1].textContent).toContain('旧空间-旧')
  })

  it('空白 workspace 视同默认空间，不聚合', () => {
    const blank = makeSession({ title: '空白空间会话', workspace: '   ' })
    renderList([blank])
    expect(document.querySelector('[data-group="workspace-tree"]')).toBeNull()
    const normalGroup = document.querySelector('[data-group="normal"]') as HTMLElement
    expect(within(normalGroup).getByText('空白空间会话')).toBeInTheDocument()
  })
})

describe('SessionList 会话树 — 顶栏交互', () => {
  it('顶栏「+」以组内最新会话的原始 workspace 触发一键新建回调', () => {
    const onCreateInWorkspace = vi.fn()
    const newer = makeSession({ title: '最新', workspace: 'D:/proj/demo', updatedAt: '2026-01-02T00:00:00Z' })
    const older = makeSession({ title: '较旧', workspace: 'D:/proj/demo/', updatedAt: '2026-01-01T00:00:00Z' })

    renderList([older, newer], onCreateInWorkspace)

    fireEvent.click(screen.getByTestId('workspace-tree-create'))
    expect(onCreateInWorkspace).toHaveBeenCalledTimes(1)
    expect(onCreateInWorkspace).toHaveBeenCalledWith('D:/proj/demo')
  })

  it('未传一键新建回调时顶栏不渲染「+」按钮', () => {
    renderList([makeSession({ title: '项目会话', workspace: 'D:/proj/demo' })])
    expect(screen.queryByTestId('workspace-tree-create')).toBeNull()
  })

  it('顶栏可折叠/展开：折叠后子会话卸载，展开后恢复', () => {
    renderList([
      makeSession({ title: '项目会话', workspace: 'D:/proj/demo' }),
    ])

    const toggle = screen.getByTestId('workspace-tree-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('项目会话')).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('项目会话')).toBeNull()

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('项目会话')).toBeInTheDocument()
  })

  it('折叠互不影响：折叠一棵树不影响另一棵的展开态', () => {
    renderList([
      makeSession({ title: '甲空间会话', workspace: 'D:/ws/a' }),
      makeSession({ title: '乙空间会话', workspace: 'D:/ws/b' }),
    ])
    const toggles = screen.getAllByTestId('workspace-tree-toggle')
    fireEvent.click(toggles[0])
    expect(screen.queryByText('甲空间会话')).toBeNull()
    expect(screen.getByText('乙空间会话')).toBeInTheDocument()
  })
})

describe('SessionList 会话树 — 与既有分组共存', () => {
  it('置顶的空间会话浮顶入置顶组、不入工作空间树（置顶优先于空间归组）', () => {
    renderList([
      makeSession({ title: '置顶项目会话', workspace: 'D:/proj/demo', pinned: true, updatedAt: '2026-01-02T00:00:00Z' }),
      makeSession({ title: '普通项目会话', workspace: 'D:/proj/demo', pinned: false, updatedAt: '2026-01-01T00:00:00Z' }),
    ])

    // 置顶会话单独成组浮顶（2026-11-06 用户裁定：置顶不受其他归组/排序干扰）
    expect(screen.getByText('已置顶')).toBeInTheDocument()
    const pinnedGroup = document.querySelector('[data-group="pinned"]') as HTMLElement
    expect(within(pinnedGroup).getByText('置顶项目会话')).toBeInTheDocument()
    expect(within(pinnedGroup).getByTestId('pin-icon')).toBeInTheDocument()
    // 树只剩未置顶会话，树内排序不受置顶影响
    const tree = document.querySelector('[data-group="workspace-tree"]') as HTMLElement
    expect(within(tree).getByTestId('workspace-tree-header').textContent).toContain('(1)')
    expect(within(tree).getByText('普通项目会话')).toBeInTheDocument()
    expect(within(tree).queryByText('置顶项目会话')).toBeNull()
  })
})

  // ===========================================================================
  // 条目标题取值优先级（缺陷修复）：会话自身标题 > 工作空间名 > 占位符
  // ===========================================================================
  describe('SessionList 条目标题 — 会话标题优先，工作空间名仅作无标题兜底', () => {
    /** 取全部条目标题文本（标题 span 与工作空间徽标同级，用 testid 精确取标题） */
    function rowTitles(): string[] {
      return screen.getAllByTestId('session-title').map((el) => (el.textContent ?? '').trim())
    }

    it('挂靠工作空间下的会话仍显示会话自身标题（不以目录名顶替）', () => {
      renderList([
        makeSession({ title: '重构登录页', workspace: 'D:/proj/demo' }),
        makeSession({ title: '排查构建失败', workspace: 'D:/ws/other' }),
      ])

      // 精确集合相等：实现若拿目录名顶替标题即红（非"包含"式宽松断言）
      expect(rowTitles()).toHaveLength(2)
      expect(new Set(rowTitles())).toEqual(new Set(['重构登录页', '排查构建失败']))
      // 目录名仍在树顶栏封面与行内徽标，分组结构不变（两个工作空间组各有一棵树）
      expect(screen.getAllByTestId('workspace-tree-header')).toHaveLength(2)
    })

    it('会话无标题时回退显示工作空间名（win/posix/尾分隔符多路径形态）', () => {
      renderList([
        makeSession({ title: '', workspace: 'D:/proj/demo', updatedAt: '2026-01-03T00:00:00Z' }),
        makeSession({ title: '', workspace: '/home/me/app', updatedAt: '2026-01-02T00:00:00Z' }),
        makeSession({ title: '', workspace: 'D:/proj/sub/', updatedAt: '2026-01-01T00:00:00Z' }),
      ])

      // 路径末段兜底：盘符路径 / posix 绝对路径 / 尾分隔符不污染末段
      expect(rowTitles()).toHaveLength(3)
      expect(new Set(rowTitles())).toEqual(new Set(['demo', 'app', 'sub']))
    })

    it('既无标题也无工作空间时显示占位符（条目不出现空标题）', () => {
      renderList([makeSession({ title: '' }), makeSession({ title: '   ' })])

      expect(rowTitles()).toEqual(['未命名会话', '未命名会话'])
      // 性质断言：任何输入下标题都非空
      expect(rowTitles().every((t) => t.length > 0)).toBe(true)
    })

    it('标题为空白串时视同无标题，回退工作空间名', () => {
      renderList([makeSession({ title: '   ', workspace: 'D:/proj/demo' })])
      expect(rowTitles()).toEqual(['demo'])
    })

    it('脏标题仍按净化结果展示（标题净化语义不回归）', () => {
      renderList([makeSession({ title: '**重构**登录页', workspace: 'D:/proj/demo' })])
      expect(rowTitles()).toEqual(['重构登录页'])
    })

    it('原有折叠结构不变：无标题会话折叠后随行卸载、展开恢复', () => {
      renderList([makeSession({ title: '', workspace: 'D:/proj/demo' })])

      const toggle = screen.getByTestId('workspace-tree-toggle')
      expect(screen.getByTestId('session-title')).toBeInTheDocument()

      fireEvent.click(toggle)
      expect(screen.queryByTestId('session-title')).toBeNull()

      fireEvent.click(toggle)
      expect(screen.getByTestId('session-title')).toBeInTheDocument()
    })

    it('条目的可访问名与可见标题同源（aria-label 反映会话标题）', () => {
      renderList([makeSession({ title: '重构登录页', workspace: 'D:/proj/demo' })])
      expect(screen.getByRole('button', { name: '会话: 重构登录页' })).toBeInTheDocument()
    })
  })
