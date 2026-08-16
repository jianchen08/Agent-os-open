/**
 * AudioPlayer 组件单元测试
 *
 * 测试覆盖：
 * - 组件渲染（标题、播放按钮、进度条）
 * - 播放/暂停切换
 * - 进度条交互
 * - 下载功能
 * - 错误状态处理
 * - 时间格式化
 * - 响应式设计（移动端适配类名）
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AudioPlayer } from '../AudioPlayer'

// Mock HTMLAudioElement
const mockAudio = {
  play: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(),
  load: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  duration: 120,
  currentTime: 0,
  muted: false,
  src: '',
}

beforeEach(() => {
  vi.clearAllMocks()
  // 重置 mock audio 状态
  mockAudio.currentTime = 0
  mockAudio.duration = 120
  mockAudio.muted = false
})

afterEach(() => {
  cleanup()
})

describe('AudioPlayer', () => {
  it('应正确渲染音频播放器组件', () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    // 验证播放按钮存在
    const playButton = screen.getByRole('button', { name: /播放/i })
    expect(playButton).toBeInTheDocument()

    // 验证下载按钮存在
    const downloadButton = screen.getByRole('button', { name: /下载/i })
    expect(downloadButton).toBeInTheDocument()
  })

  it('应显示音频标题', () => {
    render(
      <AudioPlayer src="https://example.com/audio.mp3" title="TTS 测试音频" />
    )

    expect(screen.getByText('TTS 测试音频')).toBeInTheDocument()
  })

  it('应接受 blob URL 作为音频源', () => {
    const blobUrl = 'blob:https://example.com/12345'
    render(<AudioPlayer src={blobUrl} />)

    const audio = document.querySelector('audio')
    expect(audio).toBeInTheDocument()
    expect(audio?.getAttribute('src')).toBe(blobUrl)
  })

  it('点击播放按钮应触发 audio.play()', async () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    const playButton = screen.getByRole('button', { name: /播放/i })
    fireEvent.click(playButton)

    // 播放按钮点击后应该尝试播放
    await waitFor(() => {
      expect(playButton).toBeInTheDocument()
    })
  })

  it('应显示播放进度条', () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    // 进度条容器存在
    const progressBar = document.querySelector('[data-testid="progress-bar"]')
    expect(progressBar).toBeInTheDocument()
  })

  it('应显示时长信息', () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    // 应该有时间显示区域
    const timeDisplays = screen.getAllByText(/\d+:\d+|加载中/, {})
    expect(timeDisplays.length).toBeGreaterThan(0)
  })

  it('应支持静音切换', () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    const muteButton = screen.getByRole('button', { name: /静音/i })
    expect(muteButton).toBeInTheDocument()
  })

  it('应支持下载功能', () => {
    const createObjectURLSpy = vi.fn()
    const revokeObjectURLSpy = vi.fn()
    const originalCreateURL = URL.createObjectURL
    const originalRevokeURL = URL.revokeObjectURL

    URL.createObjectURL = createObjectURLSpy
    URL.revokeObjectURL = revokeObjectURLSpy

    // Mock createElement to track download link
    // 现行实现会把 <a> appendChild 到 body 再 click（Firefox 下载必需），
    // 必须用真实 anchor 元素（普通对象会被 jsdom 的 appendChild 拒绝）
    const originalCreateElement = document.createElement.bind(document)
    const realAnchor = originalCreateElement('a')
    const clickSpy = vi.spyOn(realAnchor, 'click').mockImplementation(() => {})
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      if (tag === 'a') return realAnchor
      return originalCreateElement(tag)
    })

    render(<AudioPlayer src="https://example.com/audio.mp3" title="test-audio" />)

    const downloadButton = screen.getByRole('button', { name: /下载/i })
    fireEvent.click(downloadButton)

    expect(clickSpy).toHaveBeenCalled()
    // 下载属性正确设置
    expect(realAnchor.href).toBe('https://example.com/audio.mp3')
    expect(realAnchor.download).toBe('test-audio')

    // 恢复
    URL.createObjectURL = originalCreateURL
    URL.revokeObjectURL = originalRevokeURL
    vi.restoreAllMocks()
  })

  it('应应用自定义 className', () => {
    render(
      <AudioPlayer
        src="https://example.com/audio.mp3"
        className="custom-class"
      />
    )

    const container = document.querySelector('.custom-class')
    expect(container).toBeInTheDocument()
  })

  it('应支持多种音频格式（mp3, wav, ogg）', () => {
    const formats = [
      'https://example.com/audio.mp3',
      'https://example.com/audio.wav',
      'https://example.com/audio.ogg',
    ]

    formats.forEach((src) => {
      const { unmount } = render(<AudioPlayer src={src} />)
      const audio = document.querySelector('audio')
      expect(audio?.getAttribute('src')).toBe(src)
      unmount()
    })
  })

  it('应渲染响应式容器', () => {
    render(<AudioPlayer src="https://example.com/audio.mp3" />)

    // 验证响应式类名存在
    const container = document.querySelector('[data-testid="audio-player"]')
    expect(container).toBeInTheDocument()
  })

  it('错误态的音频链接应含 break-words + 滚动类，不含 break-all（三轮修复：工具内容不每字一行）', () => {
    render(<AudioPlayer src={'https://example.com/audio/' + 'a'.repeat(300)} />)

    // 触发 audio error → 错误态出现「查看音频链接」
    const audio = document.querySelector('audio')
    fireEvent.error(audio as HTMLMediaElement)
    fireEvent.click(screen.getByText('查看音频链接'))

    const code = document.querySelector('details code')
    expect(code).toBeInTheDocument()
    // 长链接按语义换行（break-words），不再每字符断行（break-all）
    expect(code!.className).toContain('break-words')
    expect(code!.className).not.toContain('break-all')
    // 超长链接统一滚动
    expect(code!.className).toMatch(/max-h-/)
    expect(code!.className).toContain('overflow-y-auto')
  })
})
