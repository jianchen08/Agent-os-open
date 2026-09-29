// @feature: FP-T12 前端适配(markdown 链接点击渲染链路) | @ci: frontend-test
/** @ci frontend-test */
/**
 * LobeChatMarkdown onLinkClick 通用链接拦截契约测试。
 *
 * 耦合控制（设计 §5.1）：组件对链接策略零知识——只提供通用 onLinkClick prop，
 * 返回 true = 已处理（组件 preventDefault 阻止默认导航）。
 *
 * @lobehub/ui 全家桶重——用 react-markdown 桩出真实 <a> DOM 测委托行为
 * （closest/href/textContent/preventDefault 全走真实链路）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ReactMarkdown from 'react-markdown'
import { LobeChatMarkdown } from '../LobeChatMarkdown'

// @lobehub/ui Markdown 桩：react-markdown 渲染真实锚点（委托逻辑的被测对象）
vi.mock('@lobehub/ui', () => ({
  ConfigProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Markdown: ({ children }: { children: string }) => <>{<ReactMarkdown>{children}</ReactMarkdown>}</>,
}))

function renderMd(content: string, onLinkClick?: (l: { href: string; text: string }) => boolean) {
  return render(<LobeChatMarkdown content={content} onLinkClick={onLinkClick} />)
}

function clickAnchor(linkText: string): boolean {
  const anchor = screen.getByText(linkText).closest('a')
  expect(anchor).not.toBeNull()
  const evt = new MouseEvent('click', { bubbles: true, cancelable: true })
  anchor!.dispatchEvent(evt)
  return evt.defaultPrevented
}

describe('LobeChatMarkdown onLinkClick', () => {
  it('onLinkClick 返回 true → preventDefault（默认导航被阻止）', () => {
    const onLinkClick = vi.fn(() => true)
    renderMd('[报告.docx](/uploads/abc1.docx)', onLinkClick)

    expect(clickAnchor('报告.docx')).toBe(true)
    expect(onLinkClick).toHaveBeenCalledWith({ href: '/uploads/abc1.docx', text: '报告.docx' })
  })

  it('onLinkClick 返回 false → 不阻止默认行为', () => {
    renderMd('[外链](https://example.com)', () => false)

    expect(clickAnchor('外链')).toBe(false)
  })

  it('未传 onLinkClick → 默认行为不受干预', () => {
    renderMd('[附件](/uploads/x.txt)')

    expect(clickAnchor('附件')).toBe(false)
  })

  it('非链接区域点击不触发 onLinkClick', () => {
    const onLinkClick = vi.fn(() => true)
    renderMd('普通段落文本', onLinkClick)

    fireEvent.click(screen.getByText('普通段落文本'))
    expect(onLinkClick).not.toHaveBeenCalled()
  })
})
