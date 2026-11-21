// @feature FP-0.2.shell_speech 移动端原生 ASR 桥（ShellSpeech 决策核契约）
/** @ci frontend-test */
/**
 * decideNativeSpeechError 纯函数契约测试。
 *
 * 决策域：Android 壳 ShellSpeech 桥上报的错误码（与 Web Speech 错误码同形但
 * 词表独立，浏览器路径映射不受本模块影响）→ 'ignore'（静默，Java 侧重启续听）/
 * 'fallback'（回退 server-asr）/ 错误对象（终止会话并向用户报错）。
 *
 * 区分度设计：同一错误码在「已产出文本/未产出文本」两个状态下结论相反
 * （回退会重启录音丢已识别文本，故已有产出时只报错不回退），字面值配性质断言。
 */
import { describe, expect, it } from 'vitest'
import { decideNativeSpeechError } from '../nativeSpeech'

describe('decideNativeSpeechError: 忽略类错误（Java 侧自动重启续听）', () => {
  it.each(['no-speech', 'no-match', 'aborted', 'busy'])(
    '%s → ignore，与是否已产出文本无关',
    (code) => {
      expect(decideNativeSpeechError(code, false)).toBe('ignore')
      expect(decideNativeSpeechError(code, true)).toBe('ignore')
    },
  )
})

describe('decideNativeSpeechError: 回退类（未产出文本 → 回退 server-asr）', () => {
  it.each(['network', 'server-error', 'client', 'totally-unknown-code'])(
    '%s 未产出文本 → fallback',
    (code) => {
      expect(decideNativeSpeechError(code, false)).toBe('fallback')
    },
  )
})

describe('decideNativeSpeechError: 同码分叉（已产出文本 → 报错不回退）', () => {
  it.each(['network', 'server-error', 'client'])('%s 已产出文本 → 转写失败错误对象', (code) => {
    const decision = decideNativeSpeechError(code, true)
    // 性质断言：非哨兵值、类型固定、文案非空
    expect(typeof decision).toBe('object')
    if (typeof decision === 'object') {
      expect(decision.errorType).toBe('transcription_failed')
      expect(decision.errorMessage.length).toBeGreaterThan(0)
    }
  })
})

describe('decideNativeSpeechError: 权限/设备类恒报错（不回退不忽略）', () => {
  it('not-allowed → permission_denied 错误对象，与文本产出状态无关', () => {
    const before = decideNativeSpeechError('not-allowed', false)
    const after = decideNativeSpeechError('not-allowed', true)
    expect(before).not.toBe('fallback')
    expect(before).not.toBe('ignore')
    expect(after).toEqual(before)
    if (typeof before === 'object') {
      expect(before.errorType).toBe('permission_denied')
      expect(before.errorMessage.length).toBeGreaterThan(0)
    }
  })

  it('audio-capture → transcription_failed 错误对象，与文本产出状态无关', () => {
    const before = decideNativeSpeechError('audio-capture', false)
    const after = decideNativeSpeechError('audio-capture', true)
    expect(after).toEqual(before)
    if (typeof before === 'object') {
      expect(before.errorType).toBe('transcription_failed')
    }
  })
})
