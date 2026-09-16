// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * CodeBlock 复制按钮行为测试
 *
 * 契约（用户可观察）：
 * - 点「复制」→ 代码原文进剪贴板，按钮反馈切换为「已复制」，2s 后自动复位
 * - 已处于「已复制」态再点 → 不重复写剪贴板（守卫）
 * - 剪贴板写入失败 → 按钮保持「复制」态（不误报成功），错误留痕
 *
 * 剪贴板是浏览器外部边界，按外部依赖 mock（navigator.clipboard.writeText）。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CodeBlock } from '../CodeBlock'

const writeText = vi.fn()

beforeEach(() => {
  vi.resetAllMocks()
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('CodeBlock 复制按钮', () => {
  it('复制成功：写剪贴板代码原文 + 反馈切「已复制」+ 2s 后复位', async () => {
    vi.useFakeTimers()
    writeText.mockResolvedValue(undefined)
    render(<CodeBlock code={'const a = 1\nconst b = 2'} language="typescript" />)

    expect(screen.getByText('复制')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('复制代码'))

    // 异步写剪贴板完成 → 反馈切换
    await act(async () => {})
    expect(writeText).toHaveBeenCalledWith('const a = 1\nconst b = 2')
    expect(screen.getByText('已复制')).toBeInTheDocument()
    expect(screen.queryByText('复制')).not.toBeInTheDocument()

    // 2s 复位窗口：未到点仍「已复制」，到点回「复制」
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999)
    })
    expect(screen.getByText('已复制')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(screen.getByText('复制')).toBeInTheDocument()
    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
  })

  it('已复制态再点：不重复写剪贴板（守卫）', async () => {
    vi.useFakeTimers()
    writeText.mockResolvedValue(undefined)
    render(<CodeBlock code="x = 1" />)

    fireEvent.click(screen.getByTitle('复制代码'))
    await act(async () => {})
    expect(writeText).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByTitle('复制代码'))
    await act(async () => {})
    expect(writeText).toHaveBeenCalledTimes(1)

    // 复位后再次点击：恢复可复制（守卫不粘死）
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    fireEvent.click(screen.getByTitle('复制代码'))
    await act(async () => {})
    expect(writeText).toHaveBeenCalledTimes(2)
  })

  it('写剪贴板失败：不误报成功（保持「复制」态）且错误留痕', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    writeText.mockRejectedValue(new Error('clipboard denied'))
    render(<CodeBlock code="secret" />)

    fireEvent.click(screen.getByTitle('复制代码'))

    expect(await screen.findByText('复制')).toBeInTheDocument()
    expect(screen.queryByText('已复制')).not.toBeInTheDocument()
    expect(consoleError).toHaveBeenCalled()
    consoleError.mockRestore()
  })
})
