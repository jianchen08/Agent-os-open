/**
 * UUID 生成工具测试
 *
 * 覆盖：原生 crypto.randomUUID 路径与手动回退路径（非安全上下文）。
 * 回退路径触发条件：crypto.randomUUID 不是函数（非 HTTPS 环境）。
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { generateUUID } from '@/utils/uuid'

/** 模拟非安全上下文：将 crypto.randomUUID 置为非函数 */
function simulateNoNativeRandomUUID(): void {
  Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true })
}

function restoreNativeRandomUUID(): void {
  Object.defineProperty(crypto, 'randomUUID', {
    value: crypto.randomUUID,
    configurable: true,
  })
}

describe('generateUUID', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('优先使用原生 crypto.randomUUID', () => {
    const randomUUIDSpy = vi
      .spyOn(crypto, 'randomUUID')
      .mockReturnValue('00000000-0000-4000-8000-000000000000')

    const result = generateUUID()

    expect(result).toBe('00000000-0000-4000-8000-000000000000')
    expect(randomUUIDSpy).toHaveBeenCalledTimes(1)
  })

  it('原生不可用时回退手动实现，输出合法 UUID v4', () => {
    simulateNoNativeRandomUUID()
    const mathRandomSpy = vi.spyOn(Math, 'random').mockReturnValue(0.5)

    const result = generateUUID()

    // v4 格式：8-4-4-4-12，版本位 4，变体位 8/9/a/b
    expect(result).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    expect(mathRandomSpy).toHaveBeenCalled()
    restoreNativeRandomUUID()
  })

  it('回退路径连续生成两次结果不同（随机性）', () => {
    simulateNoNativeRandomUUID()

    const a = generateUUID()
    const b = generateUUID()

    expect(a).not.toBe(b)
    restoreNativeRandomUUID()
  })
})
