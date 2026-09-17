/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
import { describe, expect, it, vi } from 'vitest'

import { isRetryableError, requestWithRetry, retry } from '../retry'

describe('isRetryableError', () => {
  it('falsy 输入一律不可重试', () => {
    for (const v of [null, undefined, false, 0, '']) {
      expect(isRetryableError(v)).toBe(false)
    }
  })

  it.each([
    // 无 response（含一切非对象错误）→ 视为网络级失败，恒可重试
    ['boom', true],
    [42, true],
    [{ message: 'Network Error', response: { status: 400 } }, true],
    [{ response: null }, true],
    [{ response: { status: 429 } }, true],
    [{ response: { status: 500 } }, true],
    [{ response: { status: 599 } }, true],
    [{ response: { status: 404 } }, false],
    [{ response: { status: 499 } }, false],
    // 无 response 时顶层 status 不参与判定（网络级短路在前）
    [{ status: 502 }, true],
    [{ status: 400 }, true],
    // 有 response 时才轮到 TypeError/AbortError/状态码分支
    [{ name: 'TypeError', message: 'fetch of /api failed', response: { status: 404 } }, true],
    [{ name: 'TypeError', message: 'cannot read property', response: { status: 404 } }, false],
    [{ name: 'AbortError', response: { status: 404 } }, true],
    [{ name: 'TimeoutError', response: { status: 404 } }, true],
  ])('输入 %j → %j', (input, expected) => {
    expect(isRetryableError(input)).toBe(expected as boolean)
  })

  it('response.status 优先于顶层 status', () => {
    // response.status=404 不可重试，即便顶层 status=500
    expect(isRetryableError({ status: 500, response: { status: 404 } })).toBe(false)
  })

  it('有 response 时非 429/5xx 一律不可重试（4xx 全谱）', () => {
    for (const status of [400, 401, 403, 409, 422, 428]) {
      expect(isRetryableError({ response: { status } })).toBe(false)
    }
  })
})

describe('retry', () => {
  it('首次成功不重试', async () => {
    const fn = vi.fn().mockResolvedValue('ok')
    await expect(retry(fn)).resolves.toBe('ok')
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('瞬时失败后重试成功（真实小延迟走 delay 路径）', async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new Error('Network Error'))
      .mockRejectedValueOnce({ response: { status: 503 } })
      .mockResolvedValue(42)
    await expect(retry(fn, { delayMs: 1 })).resolves.toBe(42)
    expect(fn).toHaveBeenCalledTimes(3)
  })

  it('不可重试错误立即抛出，不再消耗剩余次数', async () => {
    const fn = vi.fn().mockRejectedValue({ response: { status: 404 } })
    await expect(retry(fn, { maxAttempts: 5, delayMs: 1 })).rejects.toMatchObject({
      response: { status: 404 },
    })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('次数耗尽抛最后一次错误（错误身份保持）', async () => {
    const persistent = { response: { status: 500 } }
    const first = { response: { status: 500 } }
    // once 值先被消耗：第 1 次抛 first，其后恒抛 persistent，直至耗尽
    const fn = vi.fn().mockRejectedValue(persistent).mockRejectedValueOnce(first)
    await expect(retry(fn, { maxAttempts: 3, delayMs: 1 })).rejects.toBe(persistent)
    expect(fn).toHaveBeenCalledTimes(3)
  })

  it('自定义 shouldRetry 决定重试面', async () => {
    const err = new Error('x')
    const fn = vi.fn().mockRejectedValueOnce(err).mockResolvedValue('y')
    await expect(retry(fn, { delayMs: 1, shouldRetry: () => false })).rejects.toBe(err)
    expect(fn).toHaveBeenCalledTimes(1)

    const fn2 = vi.fn().mockRejectedValueOnce(err).mockResolvedValue('y')
    await expect(retry(fn2, { delayMs: 1, shouldRetry: () => true })).resolves.toBe('y')
    expect(fn2).toHaveBeenCalledTimes(2)
  })
})

describe('requestWithRetry', () => {
  it('未启用重试时直通：失败也只调一次', async () => {
    const fn = vi.fn().mockRejectedValue({ response: { status: 500 } })
    await expect(requestWithRetry(fn)).rejects.toBeTruthy()
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('启用重试按 maxRetries 上限重试后成功', async () => {
    const fn = vi
      .fn()
      .mockRejectedValueOnce(new Error('Network Error'))
      .mockResolvedValue('done')
    await expect(requestWithRetry(fn, { retry: true, maxRetries: 3, retryDelay: 1 })).resolves.toBe(
      'done',
    )
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('启用重试且持续失败：调用次数 = maxRetries', async () => {
    const fn = vi.fn().mockRejectedValue(new Error('Network Error'))
    await expect(requestWithRetry(fn, { retry: true, maxRetries: 4, retryDelay: 1 })).rejects.toThrow(
      'Network Error',
    )
    expect(fn).toHaveBeenCalledTimes(4)
  })
})
