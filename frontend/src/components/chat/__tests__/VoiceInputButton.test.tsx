// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * VoiceInputButton 语音输入按钮组件测试
 *
 * 覆盖：idle/recording/transcribing 三态 + permission_denied 错误态的
 * 图标与提示文案投影、录音态悬停变色、tooltip 3 秒自动消隐与错误切换、
 * 禁用语义（disabled / transcribing 不可点）。
 *
 * 打桩边界：仅图标聚合器（@/assets/icons，纯展示 glyph，以 testid 区分图标身份）。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { VoiceInputButton } from '../VoiceInputButton'
import type { VoiceInputError } from '@/types/voiceInput'

vi.mock('@/assets/icons', () => ({
  Loader2: (p: Record<string, unknown>) => <svg data-testid="icon-loader2" {...p} />,
  Mic: (p: Record<string, unknown>) => <svg data-testid="icon-mic" {...p} />,
  MicOff: (p: Record<string, unknown>) => <svg data-testid="icon-micoff" {...p} />,
}))

function button(): HTMLButtonElement {
  return screen.getByRole('button')
}

function permissionError(): VoiceInputError {
  return { type: 'permission_denied', message: '麦克风权限被系统拒绝' }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('VoiceInputButton 状态投影', () => {
  it.each([
    {
      name: 'idle',
      props: { state: 'idle' as const },
      title: '语音输入',
      icon: 'icon-mic',
    },
    {
      name: 'recording',
      props: { state: 'recording' as const },
      title: '点击停止录音',
      icon: 'icon-mic',
    },
    {
      name: 'transcribing',
      props: { state: 'transcribing' as const },
      title: '正在处理...',
      icon: 'icon-loader2',
    },
    {
      name: 'permission_denied 错误',
      props: { state: 'idle' as const, error: permissionError() },
      title: '麦克风权限被拒绝',
      icon: 'icon-micoff',
    },
  ])('$name：aria/title 文案与图标身份', ({ props, title, icon }) => {
    render(<VoiceInputButton {...props} />)
    expect(button()).toHaveAttribute('title', title)
    expect(button()).toHaveAttribute('aria-label', title)
    expect(screen.getByTestId(icon)).toBeInTheDocument()
    expect(screen.queryByTestId('icon-micoff' === icon ? 'icon-mic' : 'icon-micoff')).not.toBeInTheDocument()
  })

  it('recording：三层脉冲圈渲染 + 按钮底色为红色 + 图标白色填充', () => {
    const { container } = render(<VoiceInputButton state="recording" />)
    expect(container.querySelectorAll('span[style*="voice-pulse-ring"]')).toHaveLength(2)
    expect(container.querySelectorAll('span[style*="voice-pulse-core"]')).toHaveLength(1)
    expect(button().style.backgroundColor).toBe('rgb(239, 68, 68)')
    const mic = screen.getByTestId('icon-mic')
    expect(mic).toHaveAttribute('fill', 'white')
    expect(mic.style.color).toBe('white')
  })

  it('idle：无脉冲圈、无旋转图标', () => {
    const { container } = render(<VoiceInputButton state="idle" />)
    expect(container.querySelectorAll('span[style*="voice-pulse"]')).toHaveLength(0)
    expect(screen.queryByTestId('icon-loader2')).not.toBeInTheDocument()
  })

  it('transcribing：按钮禁用不可点，图标带旋转动画，按钮呈等待光标', () => {
    const onClick = vi.fn()
    render(<VoiceInputButton state="transcribing" onClick={onClick} />)
    expect(button().disabled).toBe(true)
    expect(button().className).toContain('cursor-wait')
    expect(screen.getByTestId('icon-loader2').getAttribute('class')).toContain('animate-spin')
    fireEvent.click(button())
    expect(onClick).not.toHaveBeenCalled()
  })

  it('permission_denied：按钮呈 destructive 配色', () => {
    render(<VoiceInputButton error={permissionError()} />)
    expect(button().className).toContain('text-destructive')
  })
})

describe('VoiceInputButton 录音态悬停', () => {
  it('recording：悬停加深底色，移出恢复红色（与 idle 不变色的第二组输入）', () => {
    const { rerender } = render(<VoiceInputButton state="recording" />)
    fireEvent.mouseEnter(button())
    expect(button().style.backgroundColor).toBe('rgb(220, 38, 38)')
    fireEvent.mouseLeave(button())
    expect(button().style.backgroundColor).toBe('rgb(239, 68, 68)')

    rerender(<VoiceInputButton state="idle" />)
    fireEvent.mouseEnter(button())
    expect(button().style.backgroundColor).toBe('')
    fireEvent.mouseLeave(button())
    expect(button().style.backgroundColor).toBe('')
  })
})

describe('VoiceInputButton 错误 tooltip', () => {
  it('permission_denied：tooltip 显示错误消息，3 秒后自动消隐', () => {
    render(<VoiceInputButton error={permissionError()} />)
    expect(screen.getByText('麦克风权限被系统拒绝')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(screen.queryByText('麦克风权限被系统拒绝')).not.toBeInTheDocument()
  })

  it('非权限类错误：tooltip 照常显示消息，但 title 回退"语音输入"', () => {
    render(
      <VoiceInputButton
        error={{ type: 'recording_failed', message: '录音设备启动失败' }}
      />,
    )
    expect(screen.getByText('录音设备启动失败')).toBeInTheDocument()
    expect(button()).toHaveAttribute('title', '语音输入')
  })

  it('错误切换：新错误重置计时器并显示新消息；错误清除 tooltip 立即消失', () => {
    const { rerender } = render(<VoiceInputButton error={permissionError()} />)
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    rerender(
      <VoiceInputButton error={{ type: 'not_supported', message: '浏览器不支持语音' }} />,
    )
    expect(screen.queryByText('麦克风权限被系统拒绝')).not.toBeInTheDocument()
    expect(screen.getByText('浏览器不支持语音')).toBeInTheDocument()
    // 新错误重新计时：旧错误泄出的 1 秒不足以消隐新 tooltip
    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(screen.getByText('浏览器不支持语音')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(2000)
    })
    expect(screen.queryByText('浏览器不支持语音')).not.toBeInTheDocument()

    rerender(<VoiceInputButton />)
    expect(screen.queryByText('浏览器不支持语音')).not.toBeInTheDocument()
  })
})

describe('VoiceInputButton 交互与类名', () => {
  it.each([
    { name: 'idle', props: { state: 'idle' as const } },
    { name: 'recording', props: { state: 'recording' as const } },
    { name: '带权限错误', props: { error: permissionError() } },
  ])('$name：点击触发 onClick（一次）', ({ props }) => {
    const onClick = vi.fn()
    render(<VoiceInputButton {...props} onClick={onClick} />)
    fireEvent.click(button())
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('disabled：禁用态不可点', () => {
    const onClick = vi.fn()
    render(<VoiceInputButton disabled onClick={onClick} />)
    expect(button().disabled).toBe(true)
    fireEvent.click(button())
    expect(onClick).not.toHaveBeenCalled()
  })

  it('自定义 className 合入按钮类名', () => {
    render(<VoiceInputButton className="my-extra-class" />)
    expect(button().className).toContain('my-extra-class')
  })
})
