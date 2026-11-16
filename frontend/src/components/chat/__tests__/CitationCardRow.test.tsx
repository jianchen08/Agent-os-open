// @feature FP-0.2.评审引用交互 R5：输入框引用卡片化；R6：容器内形态+评语后置 | @ci: frontend-test
/**
 * 输入框引用卡片行测试（2026-11-05 R5）。
 *
 * 契约：
 *  - 空列不渲染；有列渲染紧凑单行卡片；
 *  - kind → 不同视觉（code/doc/message 三类可辨：图标 + 徽章色 + 徽章标签）；
 *  - 摘录 CSS truncate（前缀省略）；评语文本后置渲染在摘录之后（muted 色系 +
 *    自身截断 + hover 全文；有 comment 才渲染，无标记徽章）；
 *  - × 移除同步 store；多条折叠 = 超高滚动（max-h + overflow-y-auto）；
 *  - 行自带底部分隔线与正文输入分隔，className 透传宿主预留位（R6 容器内形态）。
 */
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { useChatInputStore } from '@/stores/chatInputStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { CARD_EXCERPT_DISPLAY_MAX_CHARS, CitationCardRow } from '../CitationCardRow'

const DRAFT_KEY = 'card-row-k'

type AddCitationFn = ReturnType<typeof useChatInputStore.getState>['addCitation']
type AddInput = Parameters<AddCitationFn>[1]

function add(over: Partial<AddInput>) {
  useChatInputStore.getState().addCitation(DRAFT_KEY, {
    kind: 'message',
    title: '用户',
    excerpt: '被引用文字',
    createdAt: 1710000000000,
    ...over,
  } as AddInput)
}

beforeEach(() => {
  localStorage.clear()
  useChatInputStore.setState({ citationsByDraftKey: {}, drafts: {} })
})

describe('CitationCardRow 渲染门控', () => {
  it('空列不渲染；无 draftKey 不渲染', () => {
    const { rerender } = render(<CitationCardRow draftKey={DRAFT_KEY} />)
    expect(screen.queryByTestId('citation-card-row')).toBeNull()
    add({ id: 'c1' })
    rerender(<CitationCardRow draftKey={DRAFT_KEY} />)
    expect(screen.getByTestId('citation-card-row')).toBeInTheDocument()
  })

  it('无 draftKey 时不渲染（防御）', () => {
    render(<CitationCardRow />)
    expect(screen.queryByTestId('citation-card-row')).toBeNull()
  })
})

describe('kind → 不同视觉（图标+徽章色+标签三类可辨）', () => {
  it('code/doc/message 三类徽章标签与色档互不相同', () => {
    add({ id: 'c-code', kind: 'code', title: 'src/main.py:L2', excerpt: 'line2' })
    add({ id: 'c-doc', kind: 'doc', title: 'docs/设计.md', excerpt: '章节内容' })
    add({ id: 'c-msg', kind: 'message', title: '用户', excerpt: '被引用文字' })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const cards = screen.getAllByTestId('citation-card')
    expect(cards).toHaveLength(3)
    expect(cards.map((c) => c.getAttribute('data-kind'))).toEqual(['code', 'doc', 'message'])

    // 徽章标签三类可辨
    expect(screen.getByText('代码')).toBeInTheDocument()
    expect(screen.getByText('文档')).toBeInTheDocument()
    expect(screen.getByText('消息')).toBeInTheDocument()

    // 徽章色档三类互不相同（类名字面比对）
    const badgeClass = (id: string) => {
      const card = cards.find((c) => c.getAttribute('data-card-id') === id) as HTMLElement
      return card.querySelector('[data-testid="citation-card-kind-badge"]')?.className ?? ''
    }
    const cls = [badgeClass('c-code'), badgeClass('c-doc'), badgeClass('c-msg')]
    expect(new Set(cls).size).toBe(3)
  })
})

describe('摘录与评语文本（R6：评语后置渲染；R7 Bug③：显示层截短）', () => {
  it('长摘录显示层截短至阈值+省略号（卡片不展示全文）；存储层快照仍全量（发送产物零改动）', () => {
    add({ id: 'c1', excerpt: `${'长'.repeat(300)}…` })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const excerpt = screen.getByTestId('citation-card-excerpt')
    // 显示层：前 N 字符（R8 阈值 16，前几个字符量级）+ 省略号（卡片摘录单行截断形态）
    expect(excerpt).toHaveTextContent('长'.repeat(CARD_EXCERPT_DISPLAY_MAX_CHARS))
    expect(excerpt.textContent).toHaveLength(CARD_EXCERPT_DISPLAY_MAX_CHARS + 1)
    expect(excerpt.textContent?.endsWith('…')).toBe(true)
    expect(excerpt.textContent).not.toContain('长'.repeat(CARD_EXCERPT_DISPLAY_MAX_CHARS + 1))
    expect(excerpt.className).toContain('truncate')
    expect(excerpt.className).toContain('whitespace-nowrap')
    // 存储层不变：store 快照仍为 301 字全量（显示截断只在渲染层，发送装配读快照）
    const stored = useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY]?.[0]
    expect(stored?.excerpt).toHaveLength(301)
  })

  it('短摘录（≤阈值）原样显示不截（两组有区分度输入）', () => {
    add({ id: 'c1', excerpt: '短文本摘录' })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const excerpt = screen.getByTestId('citation-card-excerpt')
    expect(excerpt.textContent).toBe('短文本摘录')
    expect(excerpt.textContent?.endsWith('…')).toBe(false)
  })

  it('有 comment → 评语文本渲染在摘录之后（muted+truncate+hover 全文）；无 comment → 不渲染', () => {
    add({ id: 'c1', comment: '措辞再收敛些' })
    add({ id: 'c2' })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const card = screen
      .getAllByTestId('citation-card')
      .find((c) => c.getAttribute('data-card-id') === 'c1') as HTMLElement
    const excerpt = card.querySelector('[data-testid="citation-card-excerpt"]') as HTMLElement
    const comment = card.querySelector('[data-testid="citation-card-comment"]') as HTMLElement
    // 评语文本直接可见（旧「评语」二字标记徽章已删——文本即标记本身）
    expect(comment).toHaveTextContent('措辞再收敛些')
    expect(screen.queryByText('评语')).toBeNull()
    // 位置契约：评语在摘录之后（同一卡片内后置）
    expect(
      excerpt.compareDocumentPosition(comment) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    // 与摘录区分：muted 色系 + 自身截断 + hover title 全文
    expect(comment.className).toContain('text-muted-foreground')
    expect(comment.className).toContain('truncate')
    expect(comment).toHaveAttribute('title', '措辞再收敛些')
    // 只有一条评语（c2 无评语不渲染）
    expect(screen.getAllByTestId('citation-card-comment')).toHaveLength(1)
  })

  it('容器内形态：行自带底部分隔线与正文输入分隔；className 透传宿主预留位', () => {
    add({ id: 'c1', excerpt: '甲' })
    render(<CitationCardRow draftKey={DRAFT_KEY} className="pr-10" />)

    const row = screen.getByTestId('citation-card-row')
    expect(row.className).toContain('border-b')
    expect(row.className).toContain('pr-10')
  })
})

describe('× 移除与折叠策略', () => {
  it('点 × → removeCitation 同步 store，列空后行消失', () => {
    add({ id: 'gone', excerpt: '甲' })
    add({ id: 'stay', excerpt: '乙' })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const removeButtons = screen.getAllByTestId('citation-card-remove')
    // 「甲」卡片上的 ×（gone 在前）
    fireEvent.click(removeButtons[0])

    const remain = useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY] ?? []
    expect(remain.map((c) => c.id)).toEqual(['stay'])
    expect(screen.getAllByTestId('citation-card')).toHaveLength(1)
    expect(screen.getByTestId('citation-card')).toHaveAttribute('data-card-id', 'stay')
  })

  it('多条折叠=超高滚动：行容器 max-h 封顶 + overflow-y-auto（选简做登记）', () => {
    for (let i = 0; i < 5; i++) add({ id: `c${i}`, excerpt: `摘录${i}` })
    render(<CitationCardRow draftKey={DRAFT_KEY} />)

    const row = screen.getByTestId('citation-card-row')
    expect(row.className).toContain('overflow-y-auto')
    expect(row.className).toContain('max-h-')
    expect(screen.getAllByTestId('citation-card')).toHaveLength(5)
  })
})
