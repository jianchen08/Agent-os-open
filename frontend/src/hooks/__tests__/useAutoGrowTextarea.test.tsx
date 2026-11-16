// @feature FP-0.2.评审引用交互 R4 件①：输入框随文本自动增高 | @ci: frontend-test
/**
 * useAutoGrowTextarea 行为测试（2026-11-05 R4 件①）。
 *
 * 行为契约：
 *  - height = auto → scrollHeight 定高：value 变化（手动输入与外部写入同路）即重算；
 *  - maxHeight 封顶：长文本高度 = 上限，不无限增长；
 *  - 短文本不超上限（= scrollHeight 本征高）。
 *
 * jsdom 无布局（scrollHeight 恒 0）：用 Object.defineProperty 在元素上桩定
 * scrollHeight getter 模拟本征内容高，断言的是 hook 对该值的换算行为
 * （auto→scrollHeight、min(scrollHeight, maxHeight)），非 jsdom 布局。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { useAutoGrowTextarea } from '../useAutoGrowTextarea'

/** 交互形态 harness：value 变化与手动输入消费点同路（value 绑 state） */
function HarnessInteractive({ maxHeight }: { maxHeight: number }) {
  const [value, setValue] = useState('')
  const ref = useAutoGrowTextarea<HTMLTextAreaElement>(value, { maxHeight })
  return (
    <textarea
      ref={ref}
      data-testid="auto-grow-ta"
      value={value}
      onChange={(e) => setValue(e.target.value)}
    />
  )
}

/** 受控形态 harness：value 由外部写入（模拟 store → 浮窗镜像同步路径） */
function HarnessControlled({ value, maxHeight }: { value: string; maxHeight: number }) {
  const ref = useAutoGrowTextarea<HTMLTextAreaElement>(value, { maxHeight })
  return <textarea ref={ref} data-testid="auto-grow-ta" value={value} onChange={() => {}} />
}

/** 在元素上桩定 scrollHeight（jsdom 无布局，getter 模拟本征内容高） */
function stubScrollHeight(el: HTMLElement, height: number) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => height })
}

describe('useAutoGrowTextarea', () => {
  it('短文本：height = scrollHeight 本征高，不超上限（未封顶）', () => {
    render(<HarnessInteractive maxHeight={96} />)
    const el = screen.getByTestId('auto-grow-ta') as HTMLTextAreaElement
    stubScrollHeight(el, 32)

    fireEvent.change(el, { target: { value: '短' } })

    expect(el.style.height).toBe('32px')
  })

  it('长文本：height 封顶 = maxHeight（不无限增长）', () => {
    render(<HarnessInteractive maxHeight={96} />)
    const el = screen.getByTestId('auto-grow-ta') as HTMLTextAreaElement
    stubScrollHeight(el, 300)

    fireEvent.change(el, { target: { value: '很长的一行'.repeat(50) } })

    expect(el.style.height).toBe('96px')
  })

  it('外部写入（value prop 变化）同样重算：两次有区分度输入高度不同', () => {
    const { rerender } = render(<HarnessControlled value="一" maxHeight={200} />)
    const el = screen.getByTestId('auto-grow-ta') as HTMLTextAreaElement
    stubScrollHeight(el, 40)
    rerender(<HarnessControlled value="一行内容" maxHeight={200} />)
    expect(el.style.height).toBe('40px')

    stubScrollHeight(el, 180)
    rerender(<HarnessControlled value={'多行\n'.repeat(30)} maxHeight={200} />)
    expect(el.style.height).toBe('180px')
  })

  it('收窄回短文本：高度回落（不为峰值残留），且再超限再封顶', () => {
    render(<HarnessInteractive maxHeight={96} />)
    const el = screen.getByTestId('auto-grow-ta') as HTMLTextAreaElement
    stubScrollHeight(el, 300)
    fireEvent.change(el, { target: { value: 'peak' } })
    expect(el.style.height).toBe('96px')

    stubScrollHeight(el, 24)
    fireEvent.change(el, { target: { value: '回' } })
    expect(el.style.height).toBe('24px')

    stubScrollHeight(el, 500)
    fireEvent.change(el, { target: { value: 'again'.repeat(40) } })
    expect(el.style.height).toBe('96px')
  })
})
