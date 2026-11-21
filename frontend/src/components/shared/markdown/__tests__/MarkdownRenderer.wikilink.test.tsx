/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * MarkdownRenderer 双链渲染 + 点击跳转契约测试（AC-M2 渲染面）
 *
 * 行为断言（用户可观察）：
 * - `[[目标]]` / `[[目标|别名]]` 渲染成可点击 a[href^="/wiki/"]，别名走显示裁定；
 * - 占位写法示例与 ![[嵌入]] 降级为非链接元素且保留原始字面；
 * - 点击 /wiki/ 锚点 → 委托 openFile（解码后的仓库相对路径），不发生整窗导航；
 * - doc_id 形态 → 经反链投影 id_to_path 反查真实路径后打开（索引驱动，渲染层
 *   不发明目录约定——审查 MF-1 修复面）；未收录/反查失败/打开失败 → reportError
 *   通知中心如实上报（审查 MF-3 修复面——失败不得静默）；
 * - 普通链接（外链/相对路径）不受拦截影响。
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reportError } from '@/services/errorReporting'
import { openFile } from '@/services/fileOpener'
import { resolveDocIdPath } from '../backlinksProjection'
import { MarkdownRenderer } from '../MarkdownRenderer'

vi.mock('@/services/fileOpener', () => ({
  openFile: vi.fn(async () => ({ success: true, editor: 'builtin' as const })),
}))
vi.mock('@/services/errorReporting', () => ({ reportError: vi.fn() }))
vi.mock('../backlinksProjection', () => ({
  resolveDocIdPath: vi.fn(async () => null),
}))

describe('MarkdownRenderer — 双链渲染（AC-M2）', () => {
  beforeEach(() => {
    vi.mocked(openFile).mockClear()
    vi.mocked(openFile).mockResolvedValue({ success: true, editor: 'builtin' as const })
    vi.mocked(reportError).mockClear()
    vi.mocked(resolveDocIdPath).mockReset()
    vi.mocked(resolveDocIdPath).mockResolvedValue(null)
  })

  it('AC-M2-12 三段式渲染为可点击锚点，显示目标本身', () => {
    const { container } = render(
      <MarkdownRenderer content="契约见 [[docs/decisions/x.md|真值|设计真值]]。" />,
    )
    const anchor = container.querySelector('a[href="/wiki/docs%2Fdecisions%2Fx.md"]')
    expect(anchor).not.toBeNull()
    expect(anchor!.textContent).toBe('docs/decisions/x.md')
  })

  it('AC-M2-13 两段式别名渲染为锚点且显示别名（CJK 目标 href 正确编码）', () => {
    const { container } = render(
      <MarkdownRenderer content="配方 [[docs/working/配方.md|装机维护]] 与 [[a/b.md|显示文本]]" />,
    )
    const first = container.querySelector('a[href="/wiki/docs%2Fworking%2F%E9%85%8D%E6%96%B9.md"]')
    expect(first).not.toBeNull()
    expect(first!.textContent).toBe('装机维护')
    const second = container.querySelector('a[href="/wiki/a%2Fb.md"]')
    expect(second!.textContent).toBe('显示文本')
  })

  it('AC-M2-14 占位写法示例与嵌入降级：无锚点且字面保留', () => {
    const { container } = render(
      <MarkdownRenderer content="示例 [[目标|关系|说明]] 嵌入 ![[note]] 占位 [[doc_id]]" />,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('[[目标|关系|说明]]')
    expect(container.textContent).toContain('![[note]]')
    expect(container.textContent).toContain('[[doc_id]]')
  })

  it('AC-M2-15 点击 /wiki/ 锚点委托 openFile（解码路径），不整窗导航', () => {
    const { container } = render(
      <MarkdownRenderer content="见 [[docs/decisions/x.md|真值|说明]]" />,
    )
    const anchor = container.querySelector('a[href="/wiki/docs%2Fdecisions%2Fx.md"]')!
    const event = new MouseEvent('click', { bubbles: true, cancelable: true })
    anchor.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(openFile).toHaveBeenCalledTimes(1)
    expect(openFile).toHaveBeenCalledWith('docs/decisions/x.md')
  })

  it('AC-M2-16 点击 CJK 编码锚点：openFile 收到解码后的原始路径', () => {
    const { container } = render(<MarkdownRenderer content="[[docs/working/配方.md]]" />)
    const anchor = container.querySelector(
      'a[href="/wiki/docs%2Fworking%2F%E9%85%8D%E6%96%B9.md"]',
    )!
    fireEvent.click(anchor)
    expect(openFile).toHaveBeenCalledWith('docs/working/配方.md')
  })

  it('AC-M2-17 普通链接不受拦截：openFile 不被调用（既有行为零变化）', () => {
    const { container } = render(<MarkdownRenderer content="[普通](https://a.com) 与 [本地](docs/x.md)" />)
    // 既有行为（linkSafety 默认启用，与本改动无关）：外链 = 安全按钮，
    // 相对 URL = blocked 占位 span；两者都不产出 a[href^="/wiki/"]，openFile 不被调用
    const safetyLinks = Array.from(container.querySelectorAll('[data-streamdown="link"]'))
    expect(safetyLinks.length).toBe(1)
    const blocked = Array.from(container.querySelectorAll('span[title^="Blocked URL"]'))
    expect(blocked.length).toBe(1)
    for (const el of [...safetyLinks, ...blocked]) fireEvent.click(el)
    expect(openFile).not.toHaveBeenCalled()
  })

  it('AC-M2-18 doc_id 形态锚点 href=目标原文，点击经反链索引反查真实路径后打开（MF-1 索引驱动）', async () => {
    vi.mocked(resolveDocIdPath).mockResolvedValue(
      'docs/decisions/2025-12-05-agent-three-path-generation.md',
    )
    const { container } = render(
      <MarkdownRenderer content="决策 [[20251205-agent-three-path-generation]]" />,
    )
    // 锚点 href = doc_id 原文（渲染层不发明 docs/decisions/ 目录约定）
    const anchor = container.querySelector(
      'a[href="/wiki/20251205-agent-three-path-generation"]',
    )
    expect(anchor).not.toBeNull()
    expect(anchor!.textContent).toBe('20251205-agent-three-path-generation')
    fireEvent.click(anchor!)
    await waitFor(() => expect(resolveDocIdPath).toHaveBeenCalledWith('20251205-agent-three-path-generation'))
    await waitFor(() =>
      expect(openFile).toHaveBeenCalledWith('docs/decisions/2025-12-05-agent-three-path-generation.md'),
    )
  })

  it('AC-M2-18b doc_id 未收录（悬空/索引过期）：openFile 不被调用，reportError 上报', async () => {
    vi.mocked(resolveDocIdPath).mockResolvedValue(null)
    const { container } = render(<MarkdownRenderer content="决策 [[20251205-not-exist]]" />)
    fireEvent.click(container.querySelector('a[href="/wiki/20251205-not-exist"]')!)
    await waitFor(() => expect(reportError).toHaveBeenCalled())
    expect(openFile).not.toHaveBeenCalled()
  })

  it('AC-M2-18c 反链索引读取失败：reportError 上报可定位信息，openFile 不被调用', async () => {
    vi.mocked(resolveDocIdPath).mockRejectedValue(new Error('反向链接索引不可用'))
    const { container } = render(<MarkdownRenderer content="决策 [[20251205-agent-three-path-generation]]" />)
    fireEvent.click(container.querySelector('a[href="/wiki/20251205-agent-three-path-generation"]')!)
    await waitFor(() => expect(reportError).toHaveBeenCalledWith('反向链接索引不可用', expect.anything()))
    expect(openFile).not.toHaveBeenCalled()
  })

  it('AC-M2-18d 路径形态打开失败：reportError 上报（MF-3——失败不得静默）', async () => {
    vi.mocked(openFile).mockResolvedValueOnce({
      success: false,
      editor: 'builtin',
      message: '目标不存在',
    })
    const { container } = render(<MarkdownRenderer content="[[docs/gone.md]]" />)
    fireEvent.click(container.querySelector('a[href="/wiki/docs%2Fgone.md"]')!)
    await waitFor(() => expect(reportError).toHaveBeenCalledWith('目标不存在', expect.anything()))
  })
})
