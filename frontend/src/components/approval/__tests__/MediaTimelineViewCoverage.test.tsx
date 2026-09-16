/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * MediaTimelineView 覆盖缺口补测（与 MediaTimelineView.test.tsx 互补，不重复）
 *
 * 覆盖契约（媒体事件驱动，非内部 state 断言）：
 * - onLoadedMetadata：以元素 duration 覆盖 propDuration（含 duration 为 0 的兜底）
 * - onTimeUpdate：时间显示随元素 currentTime 更新
 * - 播放/暂停按钮：播放态显示 ⏸ 且调用 play()；暂停态显示 ▶ 且调用 pause()
 * - onEnded：回到暂停图标
 * - 时间轴点击：按时长比例定位跳转（中点 → 元素 currentTime 为时长一半）
 * - mediaDuration 为 0 时点击时间轴不跳转（除零保护）
 * - 音频模式渲染 audio 元素（无 video）
 * - 标注标记点/列表按 timestamp 升序（输入乱序也排序）
 * - formatTime 的秒数补零（<60s 与 >60s）
 *
 * 测试策略：真实组件；HTMLMediaElement.play/pause 在 jsdom 未实现（浏览器外部
 * 依赖），以元素级 stub 注入并断言调用契约；媒体事件用真实 DOM 事件派发。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MediaTimelineView } from '@/components/approval/MediaTimelineView'
import type { Annotation } from '@/types/review'

function makeAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'a1',
    type: 'video_timestamp',
    timestamp: 5,
    suggestion: '第 5 秒',
    createdAt: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

/** jsdom 未实现媒体播放：注入 stub 并返回 spy 引用 */
function stubMedia(el: HTMLMediaElement) {
  const play = vi.fn().mockResolvedValue(undefined)
  const pause = vi.fn()
  Object.defineProperty(el, 'play', { value: play, configurable: true })
  Object.defineProperty(el, 'pause', { value: pause, configurable: true })
  return { play, pause }
}

/** 直接写只读媒体属性（jsdom 的 currentTime/duration 为 getter） */
function setMediaProp(el: HTMLMediaElement, prop: 'currentTime' | 'duration', value: number) {
  Object.defineProperty(el, prop, { value, configurable: true, writable: true })
}

describe('MediaTimelineView — 媒体事件与控制', () => {
  beforeEach(() => {
    // 时间轴点击依赖 getBoundingClientRect 比例换算，jsdom 默认 0 宽 → 显式给定
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 200,
      bottom: 32,
      width: 200,
      height: 32,
      toJSON: () => ({}),
    } as DOMRect)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('loadedmetadata 以元素 duration 覆盖 prop，时间显示随之更新', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={10} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    expect(screen.getByTestId('time-display').textContent).toContain('00:10')

    setMediaProp(video, 'duration', 90)
    fireEvent.loadedMetadata(video)
    expect(screen.getByTestId('time-display').textContent).toContain('01:30')
  })

  it('loadedmetadata 的 duration 为 0 时回退 0（不显示 NaN）', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={30} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    setMediaProp(video, 'duration', 0)
    fireEvent.loadedMetadata(video)
    const display = screen.getByTestId('time-display').textContent ?? ''
    expect(display).toBe('00:00 / 00:00')
    expect(display).not.toContain('NaN')
  })

  it('timeupdate 同步当前时间显示（秒补零）', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={120} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    setMediaProp(video, 'currentTime', 65)
    fireEvent.timeUpdate(video)
    expect(screen.getByTestId('time-display').textContent).toBe('01:05 / 02:00')
  })

  it('播放按钮调用 play() 并切换为暂停图标；再点调用 pause()', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={10} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    const { play, pause } = stubMedia(video)
    const btn = screen.getByTestId('play-pause-btn')
    expect(btn.textContent).toBe('▶')

    fireEvent.click(btn)
    expect(play).toHaveBeenCalledTimes(1)
    expect(btn.textContent).toBe('⏸')

    fireEvent.click(btn)
    expect(pause).toHaveBeenCalledTimes(1)
    expect(btn.textContent).toBe('▶')
  })

  it('播放结束后回到暂停图标', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={10} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    stubMedia(video)
    fireEvent.click(screen.getByTestId('play-pause-btn'))
    expect(screen.getByTestId('play-pause-btn').textContent).toBe('⏸')

    fireEvent.ended(video)
    expect(screen.getByTestId('play-pause-btn').textContent).toBe('▶')
  })

  it('点击时间轴中点跳转到时长一半（currentTime 写入，timeupdate 后显示同步）', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={100} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    stubMedia(video)

    fireEvent.click(screen.getByTestId('timeline-bar'), { clientX: 100 })
    expect(video.currentTime).toBe(50)
    // 显示由 timeupdate 事件驱动（元素写入后由浏览器回调）
    fireEvent.timeUpdate(video)
    expect(screen.getByTestId('time-display').textContent).toContain('00:50')
  })

  it('时长为 0 时点击时间轴不跳转（除零保护）', () => {
    render(
      <MediaTimelineView mediaUrl="v.mp4" mediaType="video" duration={0} annotations={[]} />,
    )
    const video = screen.getByTestId('video-player') as HTMLVideoElement
    stubMedia(video)
    fireEvent.click(screen.getByTestId('timeline-bar'), { clientX: 100 })
    expect(video.currentTime).toBe(0)
  })

  it('音频模式渲染 audio 播放器且事件同样生效', () => {
    render(
      <MediaTimelineView mediaUrl="a.mp3" mediaType="audio" duration={8} annotations={[]} />,
    )
    const audio = screen.getByTestId('audio-player') as HTMLAudioElement
    expect(screen.queryByTestId('video-player')).toBeNull()
    const { play } = stubMedia(audio)
    fireEvent.click(screen.getByTestId('play-pause-btn'))
    expect(play).toHaveBeenCalledTimes(1)
  })
})

describe('MediaTimelineView — 标注排序与渲染', () => {
  it('标注按 timestamp 升序（输入乱序）渲染标记点与列表', () => {
    render(
      <MediaTimelineView
        mediaUrl="v.mp4"
        mediaType="video"
        duration={100}
        annotations={[
          makeAnnotation({ id: 'late', timestamp: 80, suggestion: '后' }),
          makeAnnotation({ id: 'early', timestamp: 20, suggestion: '前' }),
        ]}
      />,
    )
    const items = screen.getByTestId('annotation-list').querySelectorAll('[data-testid^="annotation-item-"]')
    expect(items[0].textContent).toContain('前')
    expect(items[1].textContent).toContain('后')
    // 标记点提示含时间与建议
    expect(screen.getByTestId('timeline-marker-0')).toHaveAttribute('title', '00:20 - 前')
  })

  it('非时间轴类型或 timestamp 缺失的标注被过滤掉', () => {
    render(
      <MediaTimelineView
        mediaUrl="v.mp4"
        mediaType="video"
        duration={100}
        annotations={[
          makeAnnotation({ id: 'img', type: 'image_region', timestamp: undefined }),
          makeAnnotation({ id: 'nostamp', timestamp: undefined }),
          makeAnnotation({ id: 'ok', timestamp: 3 }),
        ]}
      />,
    )
    const items = screen.getByTestId('annotation-list').querySelectorAll('[data-testid^="annotation-item-"]')
    expect(items).toHaveLength(1)
  })

  it('无有效标注时不渲染标注列表', () => {
    render(
      <MediaTimelineView
        mediaUrl="v.mp4"
        mediaType="video"
        duration={10}
        annotations={[makeAnnotation({ type: 'image_region' })]}
      />,
    )
    expect(screen.queryByTestId('annotation-list')).toBeNull()
  })
})
