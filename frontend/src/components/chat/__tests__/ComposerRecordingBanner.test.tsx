/** @feature FP-T12 前端适配 @vision V6 可即用 @ci frontend-test */
/**
 * ComposerRecordingBanner 行为契约测试：录音/转写/取消三态文案、时长展示、
 * 取消按钮回调与缺省不渲染。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ComposerRecordingBanner } from '../ComposerRecordingBanner'

describe('ComposerRecordingBanner', () => {
  it('未录音也未转写：零占位不渲染', () => {
    const { container } = render(
      <ComposerRecordingBanner isRecording={false} isTranscribing={false} seconds={0} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it.each<[string, number, string]>([
    ['录音中开始', 0, '00:00'],
    ['录音中跨分钟', 65, '01:05'],
  ])('录音中：%s → 「正在聆听」+ 时长 %s', (_name, seconds, durationText) => {
    render(<ComposerRecordingBanner isRecording seconds={seconds} />)
    expect(screen.getByText('正在聆听')).toBeInTheDocument()
    expect(screen.getByText(durationText)).toBeInTheDocument()
  })

  it('转写中（录音已停止）：文案为「识别中…」', () => {
    render(<ComposerRecordingBanner isRecording={false} isTranscribing seconds={12} />)
    expect(screen.getByText('识别中…')).toBeInTheDocument()
    expect(screen.queryByText('正在聆听')).not.toBeInTheDocument()
  })

  it('长按上滑取消态：文案为「松开取消」（优先于转写文案）', () => {
    render(
      <ComposerRecordingBanner isRecording isTranscribing isCancelling seconds={3} />,
    )
    expect(screen.getByText('松开取消')).toBeInTheDocument()
    expect(screen.queryByText('识别中…')).not.toBeInTheDocument()
  })

  it.each<[boolean, boolean]>([
    [true, false],
    [false, true],
  ])('录音=%s/转写=%s 时取消按钮可见并触发回调', (isRecording, isTranscribing) => {
    const onCancel = vi.fn()
    render(
      <ComposerRecordingBanner
        isRecording={isRecording}
        isTranscribing={isTranscribing}
        seconds={0}
        onCancel={onCancel}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: '取消语音输入' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('未提供取消回调：不渲染取消按钮', () => {
    render(<ComposerRecordingBanner isRecording seconds={0} />)
    expect(screen.queryByRole('button', { name: '取消语音输入' })).not.toBeInTheDocument()
  })
})
