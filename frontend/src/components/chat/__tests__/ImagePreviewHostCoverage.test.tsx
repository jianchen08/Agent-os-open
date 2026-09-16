/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ImagePreviewHost 覆盖缺口补测
 *
 * 契约：挂载后把 setSrc 注册进 toolCardRegistry 全局回调（toolCardRegistry 的
 * preview_image 协议宿主）；收到 src 后渲染全屏灯箱（role=dialog + 图片 alt）；
 * 点击遮罩或按 Escape 关闭；卸载时以 null 注销（恢复缺省新标签兜底）。
 *
 * 测试策略：真实组件 + 真实 toolCardRegistry 模块（不 mock），经
 * getGlobalImagePreviewCallback / registerGlobalImagePreviewCallback 观察注册
 * 契约（外部可观察行为），事件用真实键盘/鼠标事件。
 */

import { render, screen, act } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ImagePreviewHost } from '@/components/chat/ImagePreviewHost'
import {
  getGlobalImagePreviewCallback,
  registerGlobalImagePreviewCallback,
} from '@/utils/toolCardRegistry'

describe('ImagePreviewHost', () => {
  it('未收到预览请求时不渲染任何内容', () => {
    render(<ImagePreviewHost />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('挂载后全局预览回调可用：调用即以灯箱展示对应图片', () => {
    render(<ImagePreviewHost />)
    act(() => {
      getGlobalImagePreviewCallback()('https://example.com/a.png')
    })
    const dialog = screen.getByRole('dialog', { name: '图片预览' })
    expect(dialog).toBeInTheDocument()
    expect(screen.getByAltText('大图预览')).toHaveAttribute('src', 'https://example.com/a.png')
  })

  it('点击遮罩关闭灯箱', () => {
    render(<ImagePreviewHost />)
    act(() => {
      getGlobalImagePreviewCallback()('https://example.com/a.png')
    })
    expect(screen.getByRole('dialog', { name: '图片预览' })).toBeInTheDocument()

    act(() => {
      screen.getByRole('dialog', { name: '图片预览' }).click()
    })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Escape 关闭灯箱，其他按键不关闭', () => {
    render(<ImagePreviewHost />)
    act(() => {
      getGlobalImagePreviewCallback()('https://example.com/b.png')
    })

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }))
    })
    expect(screen.getByRole('dialog', { name: '图片预览' })).toBeInTheDocument()

    act(() => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('重复请求切换图片（第二次 src 覆盖第一次）', () => {
    render(<ImagePreviewHost />)
    act(() => {
      getGlobalImagePreviewCallback()('https://example.com/first.png')
    })
    act(() => {
      getGlobalImagePreviewCallback()('https://example.com/second.png')
    })
    expect(screen.getByAltText('大图预览')).toHaveAttribute('src', 'https://example.com/second.png')
  })

  it('卸载后全局回调注销：宿主不再拦截预览（回退缺省行为）', () => {
    const { unmount } = render(<ImagePreviewHost />)
    expect(getGlobalImagePreviewCallback()).not.toBe(registerGlobalImagePreviewCallback)
    unmount()
    // 卸载后回调不再是宿主 setState（默认兜底为新标签打开，不触发 React 渲染）
    expect(() => getGlobalImagePreviewCallback()('https://example.com/c.png')).not.toThrow()
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
