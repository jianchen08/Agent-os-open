// @feature FP-T12 前端适配 | @ci frontend-test
/**
 * CodeBlockWidget 覆盖缺口补充测试：
 * - 语法高亮分词：关键字/字符串（三引型）/行注释/尾注释/数字（含小数）/
 *   普通词/纯符号尾巴
 * - 语言标签映射与未知语言大写回退；title 优先于语言标签
 * - 行号开关；code 非字符串回退空串
 * - 复制：成功 → 「已复制」→ 2s 复位；拒绝 → execCommand 回退链
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CodeBlockWidget } from '../CodeBlockWidget'

const clipboardState: { writeText?: ReturnType<typeof vi.fn> } = {}

function setClipboard(writeText: ReturnType<typeof vi.fn> | undefined) {
  clipboardState.writeText = writeText
  Object.defineProperty(window.navigator, 'clipboard', {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  })
}

afterEach(() => {
  vi.useRealTimers()
  setClipboard(undefined)
  vi.restoreAllMocks()
})

/** 取代码区内全部 token span（跳过行号 span：其类名含 select-none） */
function tokens(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll('code span span'),
  ).filter((el) => !(el as HTMLElement).className.includes('select-none')) as HTMLElement[]
}

function tokenOf(text: string): HTMLElement | undefined {
  return tokens().find((t) => t.textContent === text)
}

describe('CodeBlockWidget 语法高亮分词', () => {
  it('python：关键字/字符串/注释/数字/普通词 各归其型', () => {
    const { container } = render(
      <CodeBlockWidget
        code={'def run(x):\n    return "ok"  # done\n    n = 42.5'}
        language="python"
      />,
    )
    expect(tokenOf('def')?.className).toContain('text-status-info')
    expect(tokenOf('return')?.className).toContain('text-status-info')
    expect(tokenOf('run')?.className).toBe('') // 普通词 = plain
    expect(tokenOf('"ok"')?.className).toContain('text-status-success')
    expect(tokenOf('42.5')?.className).toContain('text-status-warning')
    // 尾注释整段归 comment（含 # 号）
    expect(tokenOf('# done')?.className).toContain('italic')
    // 行首整行注释
    expect(container.textContent).toContain('n = 42.5')
  })

  it('行首注释整行成单 token；符号尾巴归 plain', () => {
    render(<CodeBlockWidget code={'# 标题\nx = (1)'} language="bash" />)
    expect(tokenOf('# 标题')?.className).toContain('italic')
    // 括号/等号两侧的非词符号：' = (' 与 ')' 落 plain
    expect(tokenOf(' = (')?.className).toBe('')
    expect(tokenOf(')')?.className).toBe('')
  })

  it('未知语言：无关键字表 → 单词全 plain，标签大写回退', () => {
    render(<CodeBlockWidget code="func main()" language="kotlin" />)
    expect(tokenOf('func')?.className).toBe('')
    expect(screen.getByText('KOTLIN')).toBeInTheDocument()
  })

  it('语言别名映射：py→Python；title 声明时优先于语言标签', () => {
    const { rerender } = render(<CodeBlockWidget code="x = 1" language="py" />)
    expect(screen.getByText('Python')).toBeInTheDocument()

    rerender(<CodeBlockWidget code="x = 1" language="py" title="脚本说明" />)
    expect(screen.getByText('脚本说明')).toBeInTheDocument()
    expect(screen.queryByText('Python')).toBeNull()
  })

  it('showLineNumbers=false 隐藏行号；code 非字符串回退空串', () => {
    const { container, rerender } = render(
      <CodeBlockWidget code={'a\nb'} language="text" showLineNumbers={false} />,
    )
    expect(container.querySelector('.select-none')).toBeNull()

    rerender(<CodeBlockWidget code={undefined as unknown as string} language="text" />)
    expect(document.querySelectorAll('code span span')).toHaveLength(0)
  })
})

describe('CodeBlockWidget 复制', () => {
  it('复制成功显示已复制，2 秒后复位', async () => {
    vi.useFakeTimers()
    setClipboard(vi.fn().mockResolvedValue(undefined))
    render(<CodeBlockWidget code="const x = 1" language="javascript" />)

    fireEvent.click(screen.getByRole('button', { name: /复制/ }))
    await act(async () => {}) // 微任务冲刷：clipboard.then → setCopied(true)
    expect(screen.getByText(/已复制/)).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(screen.getByText(/复制/).textContent).not.toContain('已复制')
  })

  it('clipboard 拒绝 → 回退 execCommand，仍显示已复制', async () => {
    setClipboard(vi.fn().mockRejectedValue(new Error('denied')))
    const execCommand = vi.fn(() => true)
    document.execCommand = execCommand as unknown as typeof document.execCommand
    render(<CodeBlockWidget code="print('hi')" language="python" />)

    fireEvent.click(screen.getByRole('button', { name: /复制/ }))
    await act(async () => {})
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(screen.getByText(/已复制/)).toBeInTheDocument()
    // 回退路径的临时 textarea 已清理
    expect(document.querySelector('textarea')).toBeNull()
  })
})
