/**
 * clipboard / useCopyFeedback 测试
 *
 * writeClipboard：优先 navigator.clipboard.writeText；缺失/拒绝时回退
 * execCommand('copy')；均不可用时返回 false。
 * useCopyFeedback：成功写入后 1s 置位 copied，拒绝写入不置位。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { writeClipboard } from '@/components/vendor/dsh/clipboard'
import { useCopyFeedback } from '@/components/vendor/dsh/use-copy-feedback'

describe('writeClipboard', () => {
  const originalClipboard = navigator.clipboard
  const originalExecCommand = document.execCommand

  afterEach(() => {
    Object.defineProperty(navigator, 'clipboard', { value: originalClipboard, configurable: true })
    document.execCommand = originalExecCommand
    vi.useRealTimers()
  })

  it('clipboard API 可用 → 写入成功返回 true', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await expect(writeClipboard('hello')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('hello')
  })

  it('clipboard API 拒绝（抛错）→ 返回 false', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    await expect(writeClipboard('hello')).resolves.toBe(false)
  })

  it('clipboard 缺失 → 回退 execCommand("copy") 成功返回 true', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    const exec = vi.fn(() => true)
    document.execCommand = exec as any
    await expect(writeClipboard('fallback text')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('clipboard 缺失且 execCommand 抛错 → 返回 false（textarea 已清理）', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = (() => { throw new Error('copy blocked') }) as any
    const bodyChildrenBefore = document.body.childElementCount
    await expect(writeClipboard('x')).resolves.toBe(false)
    expect(document.body.childElementCount).toBe(bodyChildrenBefore) // el 已 remove
  })

  it('clipboard 与 execCommand 均不可用 → 返回 false', async () => {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    document.execCommand = undefined as any
    await expect(writeClipboard('x')).resolves.toBe(false)
  })
})

describe('useCopyFeedback', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('写入成功后 copied 置位，1s 后自动复位', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const { result } = renderHook(() => useCopyFeedback('copy me'))
    expect(result.current.copied).toBe(false)

    act(() => { result.current.onCopy() })
    await act(async () => { await Promise.resolve() })
    expect(result.current.copied).toBe(true)

    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current.copied).toBe(false)
  })

  it('写入被拒绝 → copied 保持 false', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    const { result } = renderHook(() => useCopyFeedback('copy me'))
    act(() => { result.current.onCopy() })
    await act(async () => { await Promise.resolve() })
    expect(result.current.copied).toBe(false)
  })

  it('copied 为 true 期间重复 onCopy → 不重复写入（no-op）', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const { result } = renderHook(() => useCopyFeedback('text'))
    act(() => { result.current.onCopy() })
    await act(async () => { await Promise.resolve() })
    expect(result.current.copied).toBe(true)
    act(() => { result.current.onCopy() })
    expect(writeText).toHaveBeenCalledTimes(1)
  })
})
