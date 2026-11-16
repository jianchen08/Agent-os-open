// @feature FP-0.2.四 前端Schema @ci frontend-test
/**
 * MessageEditor 输入面字号不变量（X17-F1 P-165）：可见输入全 ≥16px（text-base）。
 * 14px 及以下的输入面会触发 iOS Safari 聚焦自动放大（M8 巡检口径）；本组件唯一
 * 输入面是编辑 textarea，断言其字号类在位且不留旧值。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageEditor } from '../MessageEditor'

describe('MessageEditor — 输入面 ≥16px 不变量（P-165）', () => {
  it('编辑 textarea 用 text-base（16px），不留 text-sm', () => {
    render(<MessageEditor content="原文" onSave={vi.fn()} onCancel={vi.fn()} />)

    const textarea = screen.getByRole('textbox')
    expect(textarea).toHaveClass('text-base')
    expect(textarea).not.toHaveClass('text-sm')
  })
})
