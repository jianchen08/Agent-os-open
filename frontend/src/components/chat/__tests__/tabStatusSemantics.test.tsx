// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * 对话标签状态语义（P1-7，MuMu 实测 shots/09「● 灵汐 0」符号含义不明）：
 * - 语义映射表完整覆盖 5 态（点色/状态词/呼吸），未知回退 unknown；
 * - 移动端 chip：状态点 + 可读状态词在场、符号退场；桌面符号契约不变。
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { AgentTabItem } from '@/components/chat/AgentTabItem'
import { TAB_STATUS_SEMANTICS, tabStatusSemantics } from '@/components/chat/tabStatusSemantics'
import type { AgentTabItemData } from '@/components/chat/AgentTabItem'
import type { AgentTabStatus } from '@/types/task'

function makeTab(status: AgentTabStatus, over: Partial<AgentTabItemData> = {}): AgentTabItemData {
  return {
    id: 't1',
    name: '灵汐',
    agentLevel: 1,
    status,
    isActive: true,
    canClose: false,
    ...over,
  }
}

function renderItem(tab: AgentTabItemData) {
  return render(<AgentTabItem tab={tab} onClick={() => {}} />)
}

afterEach(() => {
  window.innerWidth = 1024
})

describe('tabStatusSemantics — 状态语义映射表', () => {
  it('5 态全量映射：状态词非空、点色在词表内、呼吸只在进行中语义', () => {
    const statuses: AgentTabStatus[] = ['running', 'completed', 'waiting_input', 'failed', 'unknown']
    for (const s of statuses) {
      const sem = tabStatusSemantics(s)
      expect(sem.label.length).toBeGreaterThanOrEqual(2)
      expect(sem.dotClass).toMatch(/^bg-/)
      if (s === 'running' || s === 'waiting_input') expect(sem.pulsing).toBe(true)
      else expect(sem.pulsing).toBe(false)
    }
    // 区分度：不同状态词互不相同（一词一义）
    const labels = statuses.map((s) => TAB_STATUS_SEMANTICS[s].label)
    expect(new Set(labels).size).toBe(statuses.length)
  })

  it('词表外/undefined 回退 unknown 档（渲染面零分支）', () => {
    expect(tabStatusSemantics(undefined)).toBe(TAB_STATUS_SEMANTICS.unknown)
    expect(tabStatusSemantics('mystery' as AgentTabStatus)).toBe(TAB_STATUS_SEMANTICS.unknown)
  })
})

describe('AgentTabItem — 移动端语义化 chip（P1-7）', () => {
  it('移动视口：状态点 + 可读状态词在场，裸符号退场', () => {
    window.innerWidth = 375
    renderItem(makeTab('running'))
    expect(screen.getByTestId('agent-tab-status-dot')).toBeInTheDocument()
    expect(screen.getByTestId('agent-tab-status-label')).toHaveTextContent('运行中')
    expect(screen.getByTestId('agent-tab-status-dot').className).toContain('bg-primary')
    // ●✓✕ 符号位退场（tab 文本不再含符号字符）
    expect(screen.getByRole('tab').textContent).not.toContain('●')
    // 名称仍在（会话/管道标题）
    expect(screen.getByTestId('tab-label')).toHaveTextContent('灵汐')
  })

  it('failed 态显示「失败」词 + 红点（映射随状态走，非写死）', () => {
    window.innerWidth = 375
    renderItem(makeTab('failed'))
    expect(screen.getByTestId('agent-tab-status-label')).toHaveTextContent('失败')
    expect(screen.getByTestId('agent-tab-status-dot').className).toContain('bg-destructive')
  })

  it('桌面视口：符号契约不变（●/✓ 等图标位仍在，无状态词）', () => {
    renderItem(makeTab('running'))
    expect(screen.queryByTestId('agent-tab-status-dot')).toBeNull()
    expect(screen.queryByTestId('agent-tab-status-label')).toBeNull()
    expect(screen.getByRole('tab').textContent).toContain('●')
  })
})
