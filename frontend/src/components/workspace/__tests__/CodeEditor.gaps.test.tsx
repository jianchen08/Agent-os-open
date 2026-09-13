// @feature FP-T12 前端组件补测 | @ci: frontend-test
/**
 * CodeEditor 覆盖缺口补充测试：选中引用浮动按钮（代码预览/只读/Markdown 三分支、
 * 引用文本组装的行号/函数名/无行号三种形态、DOM 选区失败的 indexOf 回退）、
 * 特殊文件名语言识别（dotfile/无扩展名）、保存进行中态、非 dirty 保存兜底、
 * 大文件降级下的外部变更（内容容器已卸载）、textarea→pre 滚动同步。
 *
 * 外部依赖 mock：react-syntax-highlighter / LobeChatMarkdown（重组件）、
 * fileEditorRegistry 订阅（注入外部变更）；chatInputStore 走真实模块（内存态）。
 */

import { act, fireEvent, screen } from '@testing-library/react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { CodeEditor } from '../CodeEditor'
import { useChatInputStore } from '@/stores/chatInputStore'
import type * as fileEditorRegistry from '@/stores/fileEditorRegistry'

const onSaveMock = vi.fn()
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children, language }: { children?: React.ReactNode; language?: string }) => (
    <pre data-testid="highlighter" data-language={language}>{children}</pre>
  ),
}))
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

const subscribeMock = vi.fn((_id: string, _cb: (c: string, s?: number) => void) => () => {})
vi.mock('@/stores/fileEditorRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof fileEditorRegistry>()),
  subscribeFileChange: (id: string, cb: (c: string, s?: number) => void) =>
    subscribeMock(id, cb),
}))

const pyFile = { filePath: 'src/main.py', content: 'line1\nline2\nline3\n' }

beforeEach(() => {
  vi.clearAllMocks()
  subscribeMock.mockImplementation(() => () => {})
  onSaveMock.mockResolvedValue(true)
  useChatInputStore.setState({ pendingInsert: null })
})
afterEach(() => {
  vi.restoreAllMocks()
})

/** 在容器内对 `needle` 构造一个假 Selection（jsdom 无真实选区 API）。
 * range 暴露真实文本节点与偏移——被测代码用它回算行号；toString 返回
 * 容器内真实选中的子串（与 DOM 偏移一致，不是任意捏造值）。 */
function fakeSelect(container: Element, needle: string): Selection {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
  let node = walker.nextNode() as Text | null
  while (node) {
    const idx = node.textContent?.indexOf(needle) ?? -1
    if (idx >= 0) {
      const selected = node.textContent!.substring(idx, idx + needle.length)
      const rect = {
        left: 10, top: 20, right: 60, bottom: 32, width: 50, height: 12,
        x: 10, y: 20, toJSON: () => ({}),
      }
      const range = {
        startContainer: node,
        startOffset: idx,
        endContainer: node,
        endOffset: idx + needle.length,
        collapsed: false,
        commonAncestorContainer: node,
        getBoundingClientRect: () => rect,
      }
      return {
        isCollapsed: false,
        toString: () => selected,
        rangeCount: 1,
        getRangeAt: () => range,
        removeAllRanges: () => {},
      } as unknown as Selection
    }
    node = walker.nextNode() as Text | null
  }
  throw new Error(`text not found in container: ${needle}`)
}

function mockSelection(container: Element, needle: string) {
  vi.spyOn(window, 'getSelection').mockReturnValue(fakeSelect(container, needle))
}

/** 代码预览分支的内容容器（highlighter mock 的父 div） */
function codePreviewContainer(): HTMLElement {
  return screen.getByTestId('highlighter').parentElement as HTMLElement
}

/** 触发预览区选中 → 浮动「引用」按钮出现 */
function selectInPreview(container: HTMLElement, needle: string) {
  mockSelection(container, needle)
  fireEvent.mouseUp(container)
}

describe('CodeEditor 选中引用浮动按钮（代码预览）', () => {
  it('单行选中 → 引用到对话：带文件路径与单行号（无函数名回退路径）', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    selectInPreview(codePreviewContainer(), 'line3')

    const quoteBtn = screen.getByRole('button', { name: /引用/ })
    fireEvent.click(quoteBtn)

    // 行号来自 DOM 选区回算：line3 前有 2 个换行 → L3；向上扫描无函数定义 → 仅行号
    expect(useChatInputStore.getState().pendingInsert).toBe(
      '「src/main.py:L3:\nL3: line3」',
    )
    // 引用后按钮收起
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('跨行选中且命中函数定义 → 引用文本带函数名与 L2-L4 区间', () => {
    const content = 'def compute(x):\n    return x + 1\n\nresult = compute(2)\n'
    render(<CodeEditor filePath="src/calc.py" content={content} onSave={onSaveMock} />)
    selectInPreview(codePreviewContainer(), 'return x + 1\n\nresult = compute(2)')

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    // def compute 向上命中 → funcName；选区跨 2~4 行 → 区间格式（L起-止）；逐行带行号前缀
    expect(useChatInputStore.getState().pendingInsert).toBe(
      '「src/calc.py:compute(L2-4):\nL2: return x + 1\nL3: \nL4: result = compute(2)」',
    )
  })

  it('DOM 选区计算失败（createRange 抛错）→ indexOf 定位回退仍产出正确行号', () => {
    const content = 'alpha\nbeta\ngamma\n'
    render(<CodeEditor filePath="src/fb.py" content={content} onSave={onSaveMock} />)
    vi.spyOn(document, 'createRange').mockImplementationOnce(() => {
      throw new DOMException('range boom')
    })
    selectInPreview(codePreviewContainer(), 'beta')

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    // indexOf 命中 offset 6（'alpha\n' 后）→ L2
    expect(useChatInputStore.getState().pendingInsert).toBe('「src/fb.py:L2:\nL2: beta」')
  })

  it('选中文字不在本地内容中（回退路径未命中）→ 引用退化为仅文件路径', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    vi.spyOn(document, 'createRange').mockImplementationOnce(() => {
      throw new DOMException('range boom')
    })
    // 选区文本 'omega' 不在容器内——toString 是选区的真实产物，无需存在于 DOM
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: false,
      toString: () => 'omega',
      rangeCount: 1,
      getRangeAt: () => ({
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 10, height: 10 }),
      }),
      removeAllRanges: () => {},
    } as unknown as Selection)
    fireEvent.mouseUp(codePreviewContainer())

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    expect(useChatInputStore.getState().pendingInsert).toBe('「src/main.py:\nomega」')
  })

  it('折叠选区不弹浮动按钮', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    const container = codePreviewContainer()
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => '',
      rangeCount: 0,
      getRangeAt: () => {
        throw new Error('no ranges')
      },
      removeAllRanges: () => {},
    } as unknown as Selection)
    fireEvent.mouseUp(container)

    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('纯空白选区不弹浮动按钮', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    // isCollapsed=false 但 toString trim 后为空 → 视为无选中
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: false,
      toString: () => '   ',
      rangeCount: 1,
      getRangeAt: () => ({ getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }) }),
      removeAllRanges: () => {},
    } as unknown as Selection)
    fireEvent.mouseUp(codePreviewContainer())

    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('选中后的首次点击不关闭按钮（防误触），再次点击空白处才收起', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    const container = codePreviewContainer()
    selectInPreview(container, 'line2')
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    // 刚选中后的 click 被跳过（justSelected 防抖）
    fireEvent.click(container)
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    // 第二次点击 → 收起
    fireEvent.click(container)
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('Esc 关闭浮动按钮', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    selectInPreview(codePreviewContainer(), 'line1')
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('浮动按钮 × 关闭（代码预览分支）', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    selectInPreview(codePreviewContainer(), 'line1')
    fireEvent.click(screen.getByLabelText('关闭'))
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('切换文件时重置浮动按钮', () => {
    const { rerender } = render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    selectInPreview(codePreviewContainer(), 'line1')
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    rerender(<CodeEditor filePath="src/other.py" content="x\n" onSave={onSaveMock} />)
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })
})

describe('CodeEditor 选中引用浮动按钮（Markdown 与只读分支）', () => {
  it('Markdown 预览选中 → 浮动按钮 × 关闭', () => {
    const md = { filePath: 'docs/README.md', content: '# 标题\n\n正文段落内容\n' }
    render(<CodeEditor {...md} onSave={onSaveMock} />)
    const container = screen.getByTestId('md').parentElement as HTMLElement
    selectInPreview(container, '正文段落内容')
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('关闭'))
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })

  it('只读文件选中 → 浮动按钮 × 关闭（只读预览分支）', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} readOnly />)
    const container = codePreviewContainer()
    selectInPreview(container, 'line2')
    expect(screen.getByRole('button', { name: /引用/ })).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('关闭'))
    expect(screen.queryByRole('button', { name: /引用/ })).not.toBeInTheDocument()
  })
})

describe('CodeEditor 文件名语言识别边缘', () => {
  it('dotfile：.env 整名作扩展名 → bash（可编辑，语言入工具栏）', () => {
    render(<CodeEditor filePath="conf/.env" content="k=v" onSave={onSaveMock} />)
    expect(screen.getByText('.env')).toBeInTheDocument()
    expect(screen.getByText('bash')).toBeInTheDocument()
  })

  it('无扩展名 Makefile：语言回退 text（不可编辑 → 只读预览，语言在高亮层）', () => {
    render(<CodeEditor filePath="conf/Makefile" content="k=v" onSave={onSaveMock} />)
    expect(screen.getByText('（只读预览）')).toBeInTheDocument()
    expect(screen.getByTestId('highlighter').getAttribute('data-language')).toBe('text')
  })
})

describe('CodeEditor 保存状态边缘', () => {
  it('保存进行中：按钮显示保存中...并禁用（防重复提交）', () => {
    onSaveMock.mockReturnValue(new Promise<boolean>(() => {}))
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'draft' } })
    fireEvent.click(screen.getByRole('button', { name: /^保存$/ }))

    expect(screen.getByRole('button', { name: /保存中/ })).toBeDisabled()
  })

  it('非 dirty 时点击保存（禁用态兜底触发）不调用保存回调', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    const saveBtn = screen.getByRole('button', { name: /^保存$/ })
    expect(saveBtn).toBeDisabled()
    fireEvent.click(saveBtn)
    expect(onSaveMock).not.toHaveBeenCalled()
  })
})

describe('CodeEditor 外部变更与滚动', () => {
  it('大文件降级（内容容器已卸载）时外部变更静默同步不崩溃', () => {
    let notify: ((c: string, s?: number) => void) | undefined
    subscribeMock.mockImplementation((_id, cb) => {
      notify = cb
      return () => {}
    })
    const { rerender } = render(
      <CodeEditor {...pyFile} tabId="tab-large" onSave={onSaveMock} />,
    )
    rerender(
      <CodeEditor {...pyFile} content="x" size={2_000_000} tabId="tab-large" onSave={onSaveMock} />,
    )
    expect(screen.getByText('文件过大，无法编辑')).toBeInTheDocument()

    expect(() => act(() => notify!('fresh', 1))).not.toThrow()
    expect(screen.getByText('文件过大，无法编辑')).toBeInTheDocument()
  })

  it('textarea 滚动同步到语法高亮层 pre', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    const pre = document.querySelector('pre[aria-hidden="true"]') as HTMLElement

    textarea.scrollTop = 33
    textarea.scrollLeft = 6
    fireEvent.scroll(textarea)

    expect(pre.scrollTop).toBe(33)
    expect(pre.scrollLeft).toBe(6)
  })

  it('大文件边界：size 恰为 1MB 不降级，content 长度超 1MB（无 size）降级', () => {
    const { unmount } = render(
      <CodeEditor {...pyFile} content="x" size={1_000_000} onSave={onSaveMock} />,
    )
    expect(screen.queryByText('文件过大，无法编辑')).not.toBeInTheDocument()
    unmount()

    render(
      <CodeEditor
        {...pyFile}
        content={'x'.repeat(1_000_001)}
        onSave={onSaveMock}
      />,
    )
    expect(screen.getByText('文件过大，无法编辑')).toBeInTheDocument()
  })
})
