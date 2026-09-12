// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * CodeEditor 行为测试：模式切换 / 保存链 / 外部变更同步 / 大文件与只读分支。
 *
 * 外部依赖 mock：react-syntax-highlighter（重组件）与 LobeChatMarkdown（重组件）；
 * fileEditorRegistry / chatInputStore 走真实模块（内存态，无副作用）。
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { CodeEditor } from '../CodeEditor'
import type * as fileEditorRegistry from '@/stores/fileEditorRegistry'

const onSaveMock = vi.fn()
vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children }: { children?: React.ReactNode }) => <pre>{children}</pre>,
}))
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

const subscribeMock = vi.fn(
  (_id: string, _cb: (c: string, s?: number) => void) => () => {},
)
vi.mock('@/stores/fileEditorRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof fileEditorRegistry>()),
  subscribeFileChange: (id: string, cb: (c: string, s?: number) => void) =>
    subscribeMock(id, cb),
}))

const pyFile = { filePath: 'src/main.py', content: 'print("hi")\n' }

beforeEach(() => {
  vi.clearAllMocks()
  onSaveMock.mockResolvedValue(true)
})

describe('CodeEditor 模式与只读分支', () => {
  it('可编辑文件默认预览模式：显示编辑按钮、无 textarea', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    expect(screen.getByText('main.py')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /编辑/ })).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('点击编辑进入编辑模式：textarea 出现、保存按钮出现且因非 dirty 禁用', () => {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe('print("hi")\n')
    expect(screen.getByRole('button', { name: /保存/ })).toBeDisabled()
  })

  it('readOnly 或不可编辑扩展名 → 恒只读预览、无切换按钮', () => {
    const { unmount } = render(
      <CodeEditor {...pyFile} onSave={onSaveMock} readOnly />,
    )
    expect(screen.getByText('（只读预览）')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /编辑/ })).not.toBeInTheDocument()
    unmount()

    render(
      <CodeEditor
        filePath="assets/cover.pdf"
        content="%PDF-1.4"
        onSave={onSaveMock}
      />,
    )
    expect(screen.getByText('（只读预览）')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /编辑/ })).not.toBeInTheDocument()
  })

  it('大文件（>1MB）且可编辑 → 文件过大提示，不出编辑器', () => {
    render(
      <CodeEditor {...pyFile} content="x" size={2_000_000} onSave={onSaveMock} />,
    )
    expect(screen.getByText('文件过大，无法编辑')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})

describe('CodeEditor 保存链', () => {
  async function enterEditAndType(newValue: string) {
    render(<CodeEditor {...pyFile} onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(textarea, { target: { value: newValue } })
    return textarea
  }

  it('编辑内容 → dirty 标记出现 → 保存按钮点击回调新内容', async () => {
    await enterEditAndType('print("changed")\n')

    // dirty 星号（工具栏文件名旁）
    expect(screen.getByText('*')).toBeInTheDocument()
    const saveBtn = screen.getByRole('button', { name: /保存/ })
    expect(saveBtn).toBeEnabled()

    fireEvent.click(saveBtn)
    await waitFor(() => expect(onSaveMock).toHaveBeenCalledWith('print("changed")\n'))
    // 成功后 dirty 清除
    await waitFor(() => expect(screen.queryByText('*')).not.toBeInTheDocument())
  })

  it('保存返回 false → 显示保存失败且 dirty 保留', async () => {
    onSaveMock.mockResolvedValue(false)
    await enterEditAndType('x = 2')

    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    expect(await screen.findByText('保存失败')).toBeInTheDocument()
    expect(screen.getByText('*')).toBeInTheDocument()
  })

  it('保存抛异常 → 显示重试提示', async () => {
    onSaveMock.mockRejectedValue(new Error('io'))
    await enterEditAndType('x = 3')

    fireEvent.click(screen.getByRole('button', { name: /保存/ }))

    expect(await screen.findByText('保存失败，请重试')).toBeInTheDocument()
  })

  it('Ctrl+S 快捷键保存', async () => {
    await enterEditAndType('x = 4')
    fireEvent.keyDown(document, { key: 's', ctrlKey: true })
    await waitFor(() => expect(onSaveMock).toHaveBeenCalledWith('x = 4'))
  })
})

describe('CodeEditor 外部变更与内容同步', () => {
  it('initialContent 外部变化 → 本地内容同步且 dirty 清除', async () => {
    const { rerender } = render(
      <CodeEditor {...pyFile} onSave={onSaveMock} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'draft' } })
    expect(screen.getByText('*')).toBeInTheDocument()

    rerender(<CodeEditor {...pyFile} content="reloaded" onSave={onSaveMock} />)

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe('reloaded')
    await waitFor(() => expect(screen.queryByText('*')).not.toBeInTheDocument())
  })

  it('tabId 外部变更：无 dirty 直接同步新内容', () => {
    let notify: ((c: string, s?: number) => void) | undefined
    subscribeMock.mockImplementation((_id, cb) => {
      notify = cb
      return () => {}
    })

    render(<CodeEditor {...pyFile} tabId="tab-1" onSave={onSaveMock} />)
    expect(notify).toBeTruthy()
    act(() => notify!('fresh content', 12))

    // 预览区（SyntaxHighlighter mock）呈现新内容
    expect(screen.getByText('fresh content')).toBeInTheDocument()
  })

  it('tabId 外部变更：有 dirty 时出现提示条，覆盖后采用外部内容', () => {
    let notify: ((c: string, s?: number) => void) | undefined
    subscribeMock.mockImplementation((_id, cb) => {
      notify = cb
      return () => {}
    })

    render(<CodeEditor {...pyFile} tabId="tab-2" onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'my draft' } })

    act(() => notify!('external', 99))

    expect(screen.getByText('文件已被外部修改')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /覆盖/ }))

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe('external')
  })

  it('tabId 外部变更：点忽略保留本地草稿', () => {
    let notify: ((c: string, s?: number) => void) | undefined
    subscribeMock.mockImplementation((_id, cb) => {
      notify = cb
      return () => {}
    })

    render(<CodeEditor {...pyFile} tabId="tab-3" onSave={onSaveMock} />)
    fireEvent.click(screen.getByRole('button', { name: /编辑/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'keep me' } })

    act(() => notify!('external', 99))
    fireEvent.click(screen.getByRole('button', { name: /忽略/ }))

    const textarea = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(textarea.value).toBe('keep me')
    expect(screen.queryByText('文件已被外部修改')).not.toBeInTheDocument()
  })
})
