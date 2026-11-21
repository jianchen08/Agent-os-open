/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 截图来源派生测试（任务现场批 3，纯函数层）。
 *
 * 判据（方案 §七 批 3）：浏览器落盘路径 / 桌面 MCP base64 / 手机 data base64
 * 三条来源都能派生；非截图行不产生图；base64 被 8KB 截断时给出「需回取」信号
 * （G2 关键验收——不能静默丢图）。
 */
import { describe, expect, it } from 'vitest'
import {
  extractSceneImageSource,
  imageMimeOfPath,
  isScreenshotActivity,
} from '@/components/workspace/taskScene/sceneImageSource'
import { truncateToolResultData } from '@/utils/toolResultTruncate'
import type { SceneActivity } from '@/components/workspace/taskScene/sceneActivities'

function activity(p: Partial<SceneActivity> & { toolName: string }): SceneActivity {
  return {
    callId: 'c1',
    args: {},
    state: 'done',
    category: 'browser',
    summary: '',
    messageId: 'm1',
    ...p,
  } as SceneActivity
}

/** 一段像 base64 的假数据（长度可控） */
function fakeBase64(length: number): string {
  return 'iVBORw0KGgoAAAANSUhEUg'.repeat(Math.ceil(length / 22)).slice(0, length)
}

describe('extractSceneImageSource — 三类来源', () => {
  it('浏览器截图：取 result.file_path（落盘路径）', () => {
    const source = extractSceneImageSource(
      activity({ toolName: 'browser_take_screenshot', result: { status: 'ok', file_path: 'shots/login.png' } }),
    )
    expect(source).toEqual({ kind: 'file', path: 'shots/login.png' })
  })

  it('桌面截图：取 MCP 归一 output.images[0].base64 与 mime', () => {
    const source = extractSceneImageSource(
      activity({
        toolName: 'Screenshot',
        category: 'desktop',
        resultData: { output: { images: [{ base64: fakeBase64(200), mime_type: 'image/jpeg' }] } },
      }),
    )
    expect(source).toMatchObject({ kind: 'base64', mime: 'image/jpeg' })
  })

  it('手机截图：取 result.data（base64 原样回流）', () => {
    const source = extractSceneImageSource(
      activity({
        toolName: 'phone_control',
        category: 'phone',
        args: { action: 'screenshot' },
        resultData: { status: 'ok', data: fakeBase64(300) },
      }),
    )
    expect(source).toMatchObject({ kind: 'base64', mime: 'image/png' })
  })

  it('区分度：非截图工具无图来源（file_read / browser_click）', () => {
    expect(
      extractSceneImageSource(activity({ toolName: 'file_read', category: 'file', result: { content: 'x' } })),
    ).toBeUndefined()
    expect(
      extractSceneImageSource(
        activity({ toolName: 'browser_click', result: { status: 'ok', url: 'https://a.test' } }),
      ),
    ).toBeUndefined()
  })
})

describe('G2 —— base64 撞 8KB 截断', () => {
  it('截图结果被截断：返回 truncated 信号（含 sequence 供回取），不静默丢图', () => {
    const big = { output: { images: [{ base64: fakeBase64(20000), mime_type: 'image/png' }] } }
    const truncated = truncateToolResultData(big, 42)
    const source = extractSceneImageSource(
      activity({ toolName: 'Screenshot', category: 'desktop', resultData: truncated }),
    )
    expect(source).toMatchObject({ kind: 'truncated', sequence: 42 })
    expect(source && 'totalBytes' in source && source.totalBytes).toBeGreaterThan(8192)
  })

  it('未超阈的结果不被判截断（两组有区分度输入：小图 vs 大图）', () => {
    const small = { output: { images: [{ base64: fakeBase64(64), mime_type: 'image/png' }] } }
    const kept = truncateToolResultData(small, 7)
    const source = extractSceneImageSource(
      activity({ toolName: 'Screenshot', category: 'desktop', resultData: kept }),
    )
    expect(source?.kind).toBe('base64')
  })

  it('非截图工具即使结果被截断也不给「图」信号', () => {
    const truncated = truncateToolResultData({ text: 'x'.repeat(20000) }, 9)
    const source = extractSceneImageSource(
      activity({ toolName: 'file_read', category: 'file', resultData: truncated }),
    )
    expect(source).toBeUndefined()
  })
})

describe('辅助判定', () => {
  it('isScreenshotActivity 三类命中、其余不命中', () => {
    expect(isScreenshotActivity(activity({ toolName: 'browser_take_screenshot' }))).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'Screenshot' }))).toBe(true)
    expect(
      isScreenshotActivity(activity({ toolName: 'phone_control', args: { action: 'screenshot' } })),
    ).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'phone_control', args: { action: 'click' } }))).toBe(
      false,
    )
    expect(isScreenshotActivity(activity({ toolName: 'bash' }))).toBe(false)
  })

  it('imageMimeOfPath 按扩展名，未知按 png', () => {
    expect(imageMimeOfPath('a/b.webp')).toBe('image/webp')
    expect(imageMimeOfPath('a/b.JPEG')).toBe('image/jpeg')
    expect(imageMimeOfPath('a/b.unknown')).toBe('image/png')
  })
})
