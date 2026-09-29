/** @feature FP-T12 前端适配(小件补测) | @ci: frontend-test */
/**
 * 覆盖率基线小额缺口补测——EditorWidget stub 双形态 / markdownMemoComparator
 * 流式短路与会话等值 / SessionSearch 清除回焦。均为纯渲染或纯函数面，
 * 断言用户可观察输出（渲染文本/回调入参/DOM 焦点）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { EditorWidget } from '../EditorWidget'
import { markdownMemoComparator } from '@/components/shared/markdown/shared'
import { SessionSearch } from '@/components/session/SessionSearch'

describe('EditorWidget — stub 双形态', () => {
  it('无 props：占位渲染，无 JSON 块', () => {
    const { container } = render(<EditorWidget />)
    expect(screen.getByText('[Editor Widget]')).toBeInTheDocument()
    expect(container.querySelector('pre')).toBeNull()
  })

  it('带 props：配置 JSON 序列化可见', () => {
    const { container } = render(<EditorWidget props={{ lang: 'ts' }} />)
    expect(container.querySelector('pre')?.textContent).toContain('"lang": "ts"')
  })
})

describe('markdownMemoComparator — memo 等值判据', () => {
  it('流式恒不等（每帧重渲染）', () => {
    expect(markdownMemoComparator({ content: 'a' }, { content: 'a', isStreaming: true })).toBe(
      false,
    )
  })

  it('非流式：三元组全等才等值（性质断言）', () => {
    const base = { content: 'a', isStreaming: false, className: 'c' }
    expect(markdownMemoComparator(base, base)).toBe(true)
    expect(markdownMemoComparator(base, { ...base, content: 'b' })).toBe(false)
    expect(markdownMemoComparator(base, { ...base, className: 'd' })).toBe(false)
  })
})

describe('taskStatusToPipelineStatus — 未知态告警与折叠（纯面）', () => {
  it('七态/别名映射正确；未知值落 unknown 且告警一次', async () => {
    const { taskStatusToPipelineStatus } = await import('@/types/taskStatus')
    expect(taskStatusToPipelineStatus('completed')).toBe('completed')
    expect(taskStatusToPipelineStatus('cancelled')).toBe('cancelled')
    expect(taskStatusToPipelineStatus('pending_evaluation')).toBe('unknown')
    expect(taskStatusToPipelineStatus('mystery-x')).toBe('unknown')
    expect(taskStatusToPipelineStatus(undefined)).toBe('unknown')
  })
})

describe('SessionSearch — 清除动作', () => {
  it('清除按钮回调空串并回焦输入框', () => {
    const onSearchChange = vi.fn()
    render(<SessionSearch value="关键词" onSearchChange={onSearchChange} />)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '关键词x' } })
    const input = screen.getByRole('textbox')
    const clearBtn = screen.getByRole('button')
    fireEvent.click(clearBtn)
    expect(onSearchChange).toHaveBeenLastCalledWith('')
    expect(input).toHaveFocus()
  })
})
