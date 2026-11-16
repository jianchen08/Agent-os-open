/** @feature FP-RP面板v3.1刀5 | @ci: frontend-test */
/**
 * ContentCardGate · pre 门分发测试（ADR 2026-10-14-content-card-registry §3/§7）
 *
 * 兼容态锚（对照锁定）：注册表无命中 / 流式门未过 → 回投默认 CodeBlock（children
 * 原样透传，门不改变现状渲染）；命中 → ContentCardRenderer 按 form 渲染。
 * CodeBlock/MarkdownProvider 以 mock 承接（分发契约测试，不拉 lobehub 内部实现）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { ContentCardDeclaration } from '@/services/schema/contentCard'
import { ContentCardGate, extractCodeElement } from '../ContentCardGate'

vi.mock('@lobehub/ui/es/Markdown/components/MarkdownProvider', () => ({
  useMarkdownContext: vi.fn(() => ({
    animated: false,
    enableHtmlPreview: true,
    enableMermaid: true,
    fullFeaturedCodeBlock: false,
  })),
}))
vi.mock('@lobehub/ui/es/Markdown/components/CodeBlock', () => ({
  // useCode 同构提取（children=code 元素）：文本内容落到 mock 输出，供保真断言
  CodeBlock: ({ children }: { children?: unknown }) => {
    const el = (Array.isArray(children) ? children[0] : children) as
      | { props?: { children?: unknown } }
      | null
    const text =
      el && typeof el === 'object' && el.props ? String(el.props.children ?? '') : String(children ?? '')
    return (
      <pre data-testid="default-codeblock">{text}</pre>
    )
  },
}))

/** 合成 pre 门入参（react-markdown 形态：children=code 元素） */
function preChildren(lang: string, text: string) {
  return <code className={`language-${lang}`}>{text}</code>
}

function makeCard(overrides: Partial<ContentCardDeclaration> = {}): ContentCardDeclaration {
  return {
    id: 'card_x',
    match: { fences: ['xlang'] },
    form: 'css',
    pluginId: 'plugin_p',
    ...overrides,
  }
}

beforeEach(() => {
  contributionRegistry.clear()
})

describe('extractCodeElement — code 元素提取', () => {
  it('className 与文本提取；非 code 形态（空/字符串 children）→ null', () => {
    const el = preChildren('html', '<p>x</p>')
    expect(extractCodeElement(el)).toEqual({ className: 'language-html', text: '<p>x</p>' })
    expect(extractCodeElement('裸字符串')).toBeNull()
    expect(extractCodeElement(null)).toBeNull()
    expect(extractCodeElement(<code>无 language 类</code>)).toEqual({ className: undefined, text: '无 language 类' })
  })
})

describe('ContentCardGate — 分发门（类型零知识）', () => {
  it('注册表无命中 → 回投默认 CodeBlock（children 原样，现状渲染保真锚）', () => {
    render(<ContentCardGate>{preChildren('plain', 'const x = 1')}</ContentCardGate>)
    const pre = screen.getByTestId('default-codeblock')
    expect(pre).toHaveTextContent('const x = 1')
    expect(screen.queryByTestId(/content-card/)).not.toBeInTheDocument()
  })

  it('命中（webview 形态卡）→ ContentCardRenderer 沙箱 iframe 渲染（无 allow-same-origin + CSP）', () => {
    contributionRegistry.registerFirstPartyContentCards([
      makeCard({ id: 'card_html', match: { fences: ['html'] }, form: 'webview' }),
    ])
    const { container } = render(<ContentCardGate>{preChildren('html', '<p>hi</p>')}</ContentCardGate>)

    const card = container.querySelector('[data-content-card="card_html"]')
    expect(card).not.toBeNull()
    const iframe = card!.querySelector('iframe')!
    expect(iframe).not.toBeNull()
    expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(iframe.getAttribute('srcdoc')).toContain('<p>hi</p>')
    expect(iframe.getAttribute('srcdoc')).toContain('Content-Security-Policy')
    // 卡容器无裸 dangerouslySetInnerHTML 面：fence 内容只出现在 srcdoc（沙箱内）
    expect(card!.querySelector('[data-testid="content-card-css-body"]')).toBeNull()
  })

  it('流式门：isStreaming=true 命中也不出卡（消息级粒度，流式原生渲染零改动）', () => {
    contributionRegistry.registerFirstPartyContentCards([makeCard()])
    render(
      <ContentCardGate isStreaming>{preChildren('xlang', '流式中的块')}</ContentCardGate>,
    )
    expect(screen.getByTestId('default-codeblock')).toHaveTextContent('流式中的块')
    expect(document.querySelector('[data-content-card]')).toBeNull()
  })

  it('css 形态：文本节点注入（HTML 标签按字面渲染不解析），声明 css 随卡注入', () => {
    contributionRegistry.registerFirstPartyContentCards([
      makeCard({ css: '.agentos-content-card pre { color: red; }' }),
    ])
    const { container } = render(
      <ContentCardGate>{preChildren('xlang', '<b>加粗标记</b> 文本')}</ContentCardGate>,
    )
    const body = screen.getByTestId('content-card-css-body')
    // 不可信内容零 HTML 解析：b 标签不出现在 DOM，文本原样
    expect(body.querySelector('b')).toBeNull()
    expect(body).toHaveTextContent('<b>加粗标记</b> 文本')
    expect(container.querySelector('style')?.textContent).toContain('.agentos-content-card pre')
  })

  it('template 形态（csv_table）：CSV 转义表格（首行表头）', () => {
    contributionRegistry.registerFirstPartyContentCards([
      makeCard({ id: 'card_csv', match: { fences: ['csv'] }, form: 'template', template: '{{csv_table}}' }),
    ])
    render(<ContentCardGate>{preChildren('csv', '名,战力\n林晚,9000\n"阿,七",1')}</ContentCardGate>)
    const table = document.querySelector('[data-content-card="card_csv"] table')!
    expect(table.querySelectorAll('thead th')).toHaveLength(2)
    expect(Array.from(table.querySelectorAll('tbody td')).map((c) => c.textContent)).toEqual([
      '林晚',
      '9000',
      '阿,七',
      '1',
    ])
  })

  it('折叠切换：源码视图=默认 CodeBlock 原样，渲染视图=卡体（default_collapsed 双态）', () => {
    contributionRegistry.registerFirstPartyContentCards([makeCard()])
    const { rerender } = render(
      <ContentCardGate>{preChildren('xlang', '块内容')}</ContentCardGate>,
    )
    // 缺省渲染视图（default_collapsed 缺省 false）
    expect(screen.queryByTestId('default-codeblock')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('content-card-toggle'))
    expect(screen.getByTestId('default-codeblock')).toHaveTextContent('块内容')

    // default_collapsed=true 声明 → 缺省源码视图（first-party 段跨 clear 存续是
    // 契约行为，本例换 fence 避免与上一注册互扰）
    contributionRegistry.registerFirstPartyContentCards([
      makeCard({ id: 'card_collapsed', match: { fences: ['ycoll'] }, default_collapsed: true }),
    ])
    rerender(<ContentCardGate>{preChildren('ycoll', '块内容')}</ContentCardGate>)
    expect(screen.getByTestId('default-codeblock')).toBeInTheDocument()
    expect(document.querySelector('[data-content-card="card_collapsed"]')).not.toBeNull()
  })

  it('分层边界：message_cards / content_cards 互不干扰（content 卡命中不依赖消息形态）', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_roleplay',
          contributes: {
            message_cards: [{ match: { kind: 'bubble', modes: ['roleplay'] }, style_id: 'roleplay_bubble_card' }],
          },
        },
      ],
    })
    contributionRegistry.registerFirstPartyContentCards([makeCard()])
    const { container } = render(<ContentCardGate>{preChildren('xlang', '内容')}</ContentCardGate>)
    // message_cards 声明不进内容卡查询源
    expect(contributionRegistry.getAllMessageCards()).toHaveLength(1)
    expect(container.querySelector('[data-content-card="card_x"]')).not.toBeNull()
  })
})
