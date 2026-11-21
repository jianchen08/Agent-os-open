/** @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
 * 输入区错误提示折叠三态（移动端视觉 P1-6，MuMu 实测 shots/07 整屏红字墙）：
 * 折叠态（默认，一行摘要短语）/ 展开态（全文 + 复制）/ 收起回折叠。复制走
 * 既有 clipboard + toast 面。摘要短语提炼规则单测同文件（纯函数直测）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { ChatInputErrorAlert } from '../ChatInputErrorAlert'
import { summarizeErrorMessage } from '../errorAlertSummary'

/** MuMu 实拍同构长文案（URL 断行、多层冒号） */
const LONG_ASR_ERROR =
  '语音转文字失败：ASR 转写失败（provider 链全部失败）:local: 本地模型下载失败 ' +
  '(https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx): ' +
  '<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>'

function mount(message = LONG_ASR_ERROR) {
  return render(<ChatInputErrorAlert message={message} onClose={() => {}} />)
}

describe('ChatInputErrorAlert — 折叠三态（P1-6）', () => {
  it('折叠态（默认）：只显一行摘要短语，全文不在场，详情按钮在场', () => {
    mount()
    expect(screen.getByTestId('input-error-summary')).toHaveTextContent('语音转文字失败')
    expect(screen.queryByTestId('input-error-full')).not.toBeInTheDocument()
    expect(screen.queryByTestId('input-error-copy')).not.toBeInTheDocument()
    expect(screen.getByTestId('input-error-expand')).toBeInTheDocument()
  })

  it('展开态：点「详情」后全文在场 + 复制按钮在场', () => {
    mount()
    fireEvent.click(screen.getByTestId('input-error-expand'))
    expect(screen.getByTestId('input-error-full')).toHaveTextContent('CERTIFICATE_VERIFY_FAILED')
    expect(screen.getByTestId('input-error-copy')).toBeInTheDocument()
    expect(screen.getByTestId('input-error-collapse')).toBeInTheDocument()
  })

  it('复制：把完整错误文案写进剪贴板（toast 反馈面由 sonner 承载）', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mount()
    fireEvent.click(screen.getByTestId('input-error-expand'))
    fireEvent.click(screen.getByTestId('input-error-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(LONG_ASR_ERROR))
  })

  it('收起：点「收起」回折叠态（全文退场、摘要回场）', () => {
    mount()
    fireEvent.click(screen.getByTestId('input-error-expand'))
    fireEvent.click(screen.getByTestId('input-error-collapse'))
    expect(screen.queryByTestId('input-error-full')).not.toBeInTheDocument()
    expect(screen.getByTestId('input-error-summary')).toBeInTheDocument()
  })
})

describe('summarizeErrorMessage — 摘要短语提炼', () => {
  it('首个中文冒号前短语（MuMu 实拍 ASR 错误）', () => {
    expect(summarizeErrorMessage(LONG_ASR_ERROR)).toBe('语音转文字失败')
  })

  it('ASCII 冒号后带空白才算分隔符（前片段成短语）', () => {
    expect(summarizeErrorMessage('Network error: connection refused')).toBe('Network error')
  })

  it('URL 裸冒号（无空白跟随）不把协议头劈成短语', () => {
    expect(summarizeErrorMessage('https://hf-mirror.com 不可达')).toBe(
      'https://hf-mirror.com 不可达',
    )
  })

  it('短前片段（≥2 字符）直接作为短语', () => {
    expect(summarizeErrorMessage('local: 本地模型下载失败')).toBe('local')
  })

  it('URL 裸冒号（无空白跟随）不劈段：合法分隔符劈出的是完整 URL 而非「https」', () => {
    // 反例锚定：若把裸冒号当分隔符，URL 开头的消息会被劈成「https」——防拟合
    expect(summarizeErrorMessage('https://hf-mirror.com/model.onnx: 下载失败')).toBe(
      'https://hf-mirror.com/model.onnx',
    )
  })

  it('无分隔符消息原样返回', () => {
    expect(summarizeErrorMessage('文件上传失败')).toBe('文件上传失败')
  })

  it('超长无分隔符消息截断加省略号', () => {
    const long = '很长的错误'.repeat(30)
    const out = summarizeErrorMessage(long)
    expect(out.length).toBeLessThanOrEqual(41)
    expect(out.endsWith('…')).toBe(true)
  })

  it('空串安全', () => {
    expect(summarizeErrorMessage('')).toBe('')
  })
})
