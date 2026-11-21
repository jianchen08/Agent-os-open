/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 现场截图缩略图测试（任务现场批 3，视图层）。
 *
 * 判据（方案 §七 批 3 / 组件头注释）：
 * 1. base64 来源直渲染 data URL 缩略图，点击开灯箱看原图（与对话卡同交互）
 * 2. 截断态给「图」角标，点击走 expandTruncatedToolResult 回取；
 *    回取中显示「取图中」（重复点击不重入），失败如实显示「图待回取」（不假装有图）
 * 3. 图片解码失败降级「截图」角标（不渲染破图）
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SceneThumbnail } from '@/components/workspace/taskScene/SceneThumbnail'
import { expandTruncatedToolResult } from '@/utils/toolResultBackfill'
import { truncateToolResultData } from '@/utils/toolResultTruncate'
import type { SceneActivity } from '@/components/workspace/taskScene/sceneActivities'

vi.mock('@/utils/toolResultBackfill', () => ({
  expandTruncatedToolResult: vi.fn(),
}))

const mockExpand = vi.mocked(expandTruncatedToolResult)

function activity(p: Partial<SceneActivity> & { toolName: string }): SceneActivity {
  return {
    callId: 'call-snap-1',
    args: {},
    state: 'done',
    category: 'desktop',
    summary: '',
    messageId: 'msg-1',
    ...p,
  } as SceneActivity
}

/** 一段像 base64 的假数据（长度可控） */
function fakeBase64(length: number): string {
  return 'iVBORw0KGgoAAAANSUhEUg'.repeat(Math.ceil(length / 22)).slice(0, length)
}

describe('SceneThumbnail — 有图来源（base64）', () => {
  it('无图来源返回 null（调用方不渲染占位）', () => {
    const { container } = render(
      <SceneThumbnail activity={activity({ toolName: 'file_read', category: 'file' })} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('base64 来源渲染 data URL 缩略图；点击开灯箱，关灯箱可再点开', async () => {
    const user = userEvent.setup()
    const data = fakeBase64(128)
    render(
      <SceneThumbnail
        activity={activity({
          toolName: 'Screenshot',
          resultData: { output: { images: [{ base64: data, mime_type: 'image/jpeg' }] } },
        })}
      />,
    )

    const thumb = screen.getByTestId('scene-thumbnail')
    const img = within(thumb).getByRole('img')
    expect(img.getAttribute('src')).toBe(`data:image/jpeg;base64,${data}`)

    await user.click(thumb)
    const lightbox = screen.getByTestId('image-lightbox')
    expect(within(lightbox).getByRole('img').getAttribute('src')).toBe(`data:image/jpeg;base64,${data}`)

    await user.click(lightbox)
    expect(screen.queryByTestId('image-lightbox')).toBeNull()
    // 关闭后缩略图仍在，可再次打开
    await user.click(screen.getByTestId('scene-thumbnail'))
    expect(screen.getByTestId('image-lightbox')).toBeInTheDocument()
  })

  it('落盘路径来源拼 workspace 取流 URL；无容器坐标降级「截图」角标', () => {
    const fileActivity = activity({
      toolName: 'browser_take_screenshot',
      category: 'browser',
      result: { status: 'ok', file_path: 'shots/login.png' },
    })
    const { unmount } = render(<SceneThumbnail activity={fileActivity} />)
    expect(screen.getByTestId('scene-thumbnail-fallback')).toBeInTheDocument()
    unmount()

    render(<SceneThumbnail activity={fileActivity} containerTaskId="task-42" />)
    const url = screen.getByTestId('scene-thumbnail').querySelector('img')?.getAttribute('src') ?? ''
    expect(url).toContain('task-42')
    expect(url).toContain(encodeURIComponent('shots/login.png'))
  })

  it('图片解码失败降级「截图」角标，不渲染破图', () => {
    render(
      <SceneThumbnail
        activity={activity({
          toolName: 'Screenshot',
          resultData: { output: { images: [{ base64: fakeBase64(128), mime_type: 'image/png' }] } },
        })}
      />,
    )
    fireEvent.error(screen.getByTestId('scene-thumbnail').querySelector('img') as HTMLElement)
    expect(screen.getByTestId('scene-thumbnail-fallback')).toBeInTheDocument()
    expect(screen.getByText('截图')).toBeInTheDocument()
    expect(screen.queryByTestId('scene-thumbnail')).toBeNull()
  })
})

describe('SceneThumbnail — 截断态回取（G2）', () => {
  beforeEach(() => {
    mockExpand.mockReset()
  })

  function truncatedActivity(): SceneActivity {
    const big = { output: { images: [{ base64: fakeBase64(20000), mime_type: 'image/png' }] } }
    return activity({
      toolName: 'Screenshot',
      resultData: truncateToolResultData(big, 42),
    })
  }

  it('截断态给「图」角标；点击按 messageId/callId/sequence 回取，成功后回到常态', async () => {
    const user = userEvent.setup()
    mockExpand.mockResolvedValue(true)
    render(<SceneThumbnail activity={truncatedActivity()} />)

    const badge = screen.getByTestId('scene-thumbnail-truncated')
    expect(within(badge).getByTestId('scene-badge')).toHaveTextContent('图')
    expect(badge.getAttribute('title')).toContain('点击回取原图')

    await user.click(badge)
    expect(mockExpand).toHaveBeenCalledWith({
      messageId: 'msg-1',
      callId: 'call-snap-1',
      sequence: 42,
    })
    // 回取成功（store 回填后 source 仍为截断标记直到上层换数据）——组件不再显示失败态
    expect(within(screen.getByTestId('scene-thumbnail-truncated')).getByTestId('scene-badge')).toHaveTextContent(
      '图',
    )
  })

  it('回取中显示「取图中」，重复点击不重入', async () => {
    const user = userEvent.setup()
    let resolveFn: (ok: boolean) => void = () => {}
    mockExpand.mockReturnValue(
      new Promise<boolean>((resolve) => {
        resolveFn = resolve
      }),
    )
    render(<SceneThumbnail activity={truncatedActivity()} />)

    const badge = screen.getByTestId('scene-thumbnail-truncated')
    await user.click(badge)
    expect(within(badge).getByTestId('scene-badge')).toHaveTextContent('取图中')

    await user.click(badge)
    expect(mockExpand).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolveFn(false)
    })
    await vi.waitFor(() =>
      expect(within(screen.getByTestId('scene-thumbnail-truncated')).getByTestId('scene-badge')).toHaveTextContent(
        '图待回取',
      ),
    )
    expect(screen.getByTestId('scene-thumbnail-truncated').getAttribute('title')).toContain('回取失败')
  })

  it('无 sequence 的截断标记回取时不带 sequence 键', async () => {
    const user = userEvent.setup()
    mockExpand.mockResolvedValue(false)
    render(
      <SceneThumbnail
        activity={activity({
          toolName: 'Screenshot',
          resultData: { __truncated: true, preview: '', totalBytes: 9000 },
        })}
      />,
    )
    await user.click(screen.getByTestId('scene-thumbnail-truncated'))
    expect(mockExpand).toHaveBeenCalledWith({ messageId: 'msg-1', callId: 'call-snap-1' })
    // 失败如实显示「图待回取」（warning 语气），不假装有图
    expect(screen.getByText('图待回取')).toBeInTheDocument()
  })
})
