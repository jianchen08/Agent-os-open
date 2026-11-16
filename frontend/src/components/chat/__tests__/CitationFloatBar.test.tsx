// @feature FP-0.2.评审引用交互 R4 件③：引用/评审浮层共通化 | @ci: frontend-test
/**
 * CitationFloatBar（选中文字浮层）行为测试（2026-11-05 R4 件③）。
 *
 * 共通组件契约（自 CodeEditor FloatingQuoteButton 上移，CodeEditor 与消息流
 * 共用）：
 *  - 「引用」→ onQuote；「评审」→ onReview(评语)，评语可选（空 = 等价纯引用）；
 *  - 评语输入框 auto-grow（R4 件① hook 接线）；
 *  - Esc 关闭，可编辑元素（评语输入框）内的 Escape 不关（输入法候选取消
 *    不是关闭语义，件②同口径）；
 *  - 浮条上 pointerDown preventDefault 防选区塌陷（评语 textarea 除外——
 *    点击输入框需要原生聚焦）；
 *  - positioning='fixed' → portal 到 body（消息流；气泡 overflow-hidden
 *    不裁剪）；默认 'absolute'（CodeEditor 容器内坐标）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CitationFloatBar } from '../CitationFloatBar'

describe('CitationFloatBar 动作与评语', () => {
  it('「引用」→ onQuote 一次', () => {
    const onQuote = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={onQuote} onReview={vi.fn()} onClose={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /引用/ }))

    expect(onQuote).toHaveBeenCalledTimes(1)
  })

  it('评语输入 + 「评审」→ onReview 收到评语原文', () => {
    const onReview = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={onReview} onClose={vi.fn()} />)

    fireEvent.change(screen.getByTestId('floating-quote-comment'), {
      target: { value: '这段推理跳步了' },
    })
    fireEvent.click(screen.getByTestId('floating-quote-review'))

    expect(onReview).toHaveBeenCalledTimes(1)
    expect(onReview).toHaveBeenCalledWith('这段推理跳步了')
  })

  it('评语为空 → onReview 收到空串（等价纯引用，由消费方组装）', () => {
    const onReview = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={onReview} onClose={vi.fn()} />)

    fireEvent.click(screen.getByTestId('floating-quote-review'))

    expect(onReview).toHaveBeenCalledWith('')
  })

  it('评语输入框 auto-grow 接线：输入后 style.height 重算封顶（max-h-24=96px）', () => {
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />)
    const el = screen.getByTestId('floating-quote-comment') as HTMLTextAreaElement
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 120 })

    fireEvent.change(el, { target: { value: '评语' } })

    expect(el.style.height).toBe('96px')
  })
})

describe('CitationFloatBar 关闭语义', () => {
  it('Esc（document 目标，非编辑区）→ onClose', () => {
    const onClose = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={onClose} />)

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('评语输入框内 Esc（可编辑目标）→ 不关（输入法候选取消不是关闭语义）', () => {
    const onClose = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={onClose} />)
    const comment = screen.getByTestId('floating-quote-comment')

    fireEvent.keyDown(comment, { key: 'Escape' })

    expect(onClose).not.toHaveBeenCalled()
  })

  it('× 按钮（aria-label=关闭）→ onClose', () => {
    const onClose = vi.fn()
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={onClose} />)

    fireEvent.click(screen.getByLabelText('关闭'))

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('CitationFloatBar 输入框点击保护区（R7 Bug①：浮条活跃期点输入框保选区保浮条）', () => {
  /** 输入框宿主容器（testid 契约真源在 ChatInput/FullscreenComposer，本套件桩等形） */
  function inputBoxStub(testid: string) {
    return (
      <div data-testid={testid}>
        <textarea data-testid={`${testid}-textarea`} />
      </div>
    )
  }

  it('mousedown 落主输入框容器（chat-input-box）→ preventDefault（挡默认清选区+原生聚焦）+ 手动聚焦 textarea', () => {
    render(
      <>
        {inputBoxStub('chat-input-box')}
        <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />
      </>,
    )
    const textarea = screen.getByTestId('chat-input-box-textarea')

    const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    textarea.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(textarea)
  })

  it('mousedown 落全屏浮窗容器（fullscreen-composer / fullscreen-composer-input-box）→ 同保护区', () => {
    for (const testid of ['fullscreen-composer', 'fullscreen-composer-input-box']) {
      const { unmount } = render(
        <>
          {inputBoxStub(testid)}
          <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />
        </>,
      )
      const textarea = screen.getByTestId(`${testid}-textarea`)

      const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
      textarea.dispatchEvent(evt)

      expect(evt.defaultPrevented).toBe(true)
      expect(document.activeElement).toBe(textarea)
      unmount()
    }
  })

  it('容器内非可编辑非交互区（容器本体）→ preventDefault + 焦点回退容器内 textarea（可打字）', () => {
    render(
      <>
        {inputBoxStub('chat-input-box')}
        <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />
      </>,
    )
    const box = screen.getByTestId('chat-input-box')
    const textarea = screen.getByTestId('chat-input-box-textarea')

    const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    box.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(textarea)
  })

  it('容器内交互件（按钮）→ preventDefault 但不抢焦点（click 语义不变）', () => {
    render(
      <>
        <div data-testid="chat-input-box">
          <button type="button" data-testid="chat-input-box-button">动作</button>
          <textarea data-testid="chat-input-box-textarea" />
        </div>
        <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />
      </>,
    )
    const button = screen.getByTestId('chat-input-box-button')
    const textarea = screen.getByTestId('chat-input-box-textarea')

    const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    button.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(true)
    expect(document.activeElement).not.toBe(textarea)
  })

  it('mousedown 落输入框外（浮条上/其他区域）→ 不 preventDefault（其余区域行为不变）', () => {
    render(
      <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />,
    )
    const bar = screen.getByTestId('citation-float-bar')
    const elsewhere = document.createElement('div')
    document.body.appendChild(elsewhere)

    const barEvt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    bar.dispatchEvent(barEvt)
    const outEvt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    elsewhere.dispatchEvent(outEvt)

    expect(barEvt.defaultPrevented).toBe(false)
    expect(outEvt.defaultPrevented).toBe(false)
    elsewhere.remove()
  })

  it('浮条关闭（卸载）→ 保护区随卸载移除：mousedown 不再 preventDefault', () => {
    const { unmount } = render(
      <>
        {inputBoxStub('chat-input-box')}
        <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />
      </>,
    )
    unmount()
    // 卸载后原节点脱离文档树（事件不再冒泡到 document），探针桩一个活节点
    const probe = document.createElement('div')
    probe.setAttribute('data-testid', 'chat-input-box')
    const probeTextarea = document.createElement('textarea')
    probe.appendChild(probeTextarea)
    document.body.appendChild(probe)

    const evt = new MouseEvent('mousedown', { bubbles: true, cancelable: true })
    probeTextarea.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(false)
    probe.remove()
  })
})

describe('CitationFloatBar 选区保护与定位', () => {
  it('浮条上 pointerDown preventDefault（防点击时文档选区塌陷）', () => {
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />)
    const bar = screen.getByTestId('citation-float-bar')

    const evt = new MouseEvent('pointerdown', { bubbles: true, cancelable: true })
    bar.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(true)
  })

  it('评语 textarea 上 pointerDown 放行（不 preventDefault，保原生聚焦）', () => {
    render(<CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />)
    const comment = screen.getByTestId('floating-quote-comment')

    const evt = new MouseEvent('pointerdown', { bubbles: true, cancelable: true })
    comment.dispatchEvent(evt)

    expect(evt.defaultPrevented).toBe(false)
  })

  it('positioning=fixed → portal 到 body（消息流：气泡 overflow-hidden 不裁剪）', () => {
    render(<CitationFloatBar positioning="fixed" position={{ x: 200, y: 300 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />)

    const bar = screen.getByTestId('citation-float-bar')
    expect(bar.parentElement).toBe(document.body)
    expect(bar.className).toContain('fixed')
  })

  it('默认 absolute → 原位渲染（CodeEditor 容器内坐标契约）', () => {
    const { container } = render(
      <CitationFloatBar position={{ x: 100, y: 80 }} onQuote={vi.fn()} onReview={vi.fn()} onClose={vi.fn()} />,
    )
    const bar = screen.getByTestId('citation-float-bar')
    expect(bar.className).toContain('absolute')
    expect(container.contains(bar)).toBe(true)
  })
})
