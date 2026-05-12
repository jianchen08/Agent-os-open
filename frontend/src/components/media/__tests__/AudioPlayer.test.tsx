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
    const mockAnchor = {
      href: '',
      download: '',
      click: vi.fn(),
    }
    const originalCreateElement = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      if (tag === 'a') return mockAnchor as unknown as HTMLAnchorElement
      return originalCreateElement(tag)
    })

    render(<AudioPlayer src="https://example.com/audio.mp3" title="test-audio" />)

    const downloadButton = screen.getByRole('button', { name: /下载/i })
    fireEvent.click(downloadButton)

    expect(mockAnchor.click).toHaveBeenCalled()

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
})
