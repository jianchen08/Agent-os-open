/**
 * MediaRenderer 组件「工具内容展示」回归测试
 *
 * 覆盖三轮修复目标：
 * - 媒体/文件路径（输入参数展示）不再每字一行：break-all → break-words
 * - 超长媒体链接统一滚动：max-h + overflow-y-auto（无论哪个层级展开）
 * - 全仓无 break-all 残留（工具内容相关）
 *
 * 断言可观察行为（DOM 类名契约 + 文本可见性）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { AudioRenderer, VideoRenderer } from '../MediaRenderer'

afterEach(() => {
  document.body.innerHTML = ''
})

/** 触发 audio/video 元素 error 事件，让组件进入错误态（错误态展示链接详情） */
function triggerMediaError(selector: 'audio' | 'video') {
  const el = document.querySelector(selector)
  expect(el).not.toBeNull()
  fireEvent.error(el as HTMLMediaElement)
}

describe('AC-工具卡片UI-三轮: MediaRenderer 媒体链接展示（break-words + 滚动）', () => {
  it('AudioRenderer 错误态的媒体链接应含 break-words + 滚动类，不含 break-all', () => {
    render(<AudioRenderer src={'https://example.com/audio/' + 'a'.repeat(300)} />)

    // 触发 error → 错误态出现「查看音频链接」
    triggerMediaError('audio')
    fireEvent.click(screen.getByText('查看音频链接'))

    const code = document.querySelector('details code')
    expect(code).toBeInTheDocument()
    // break-all 副作用已清除：中文/长串按语义换行而非每字符断行
    expect(code!.className).toContain('break-words')
    expect(code!.className).not.toContain('break-all')
    // 超长链接统一滚动：max-h + overflow-y-auto（任何层级展开都生效）
    expect(code!.className).toMatch(/max-h-/)
    expect(code!.className).toContain('overflow-y-auto')
  })

  it('VideoRenderer 错误态的媒体链接应含 break-words + 滚动类，不含 break-all', () => {
    render(<VideoRenderer src={'https://example.com/video/' + 'v'.repeat(300)} />)

    triggerMediaError('video')
    fireEvent.click(screen.getByText('查看视频链接'))

    const code = document.querySelector('details code')
    expect(code).toBeInTheDocument()
    expect(code!.className).toContain('break-words')
    expect(code!.className).not.toContain('break-all')
    expect(code!.className).toMatch(/max-h-/)
    expect(code!.className).toContain('overflow-y-auto')
  })
})
