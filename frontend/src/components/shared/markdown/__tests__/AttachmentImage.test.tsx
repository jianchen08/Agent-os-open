/** @ci frontend-test */
/**
 * markdown 图片附件组件测试（失败显式化 2026-09-08 + 鉴权取流 ADR 2026-10-10）。
 *
 * 行为契约：
 * - /uploads src 经鉴权内容端点取流（apiClient 带 Authorization → blob），
 *   组件渲染解析后的 objectURL，不把直链交给 <img>（直链无法携带凭据）；
 * - 非 /uploads src（https:/data:）透传，行为不变；
 * - 取流失败（404/401 等）或解码 onError → 显式失败卡（失败说明 + 附件名）；
 * - src 变化后失败态自动复位。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AttachmentImage } from '../AttachmentImage'

// AttachmentImage 以子路径引用 @lobehub/ui Image（bundle 面）——mock 对准该
// 子路径，以最小 <img> 替身承担其成功路径渲染契约。
vi.mock('@lobehub/ui/es/Image/index', () => ({
  default: ({
    src,
    alt,
    onError,
  }: {
    src?: string
    alt?: string
    onError?: (e: Event) => void
  }) => <img src={src} alt={alt} onError={onError} data-testid="lobehub-image" />,
}))

// mock apiClient（外部 HTTP 依赖）：/uploads 取流统一走鉴权内容端点
const getMock = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => getMock(...args) },
}))

// blob URL（jsdom 无 createObjectURL，桩住解析产物）
const createObjectURLMock = vi.fn(() => 'blob:mock-1')
vi.stubGlobal('URL', {
  ...URL,
  createObjectURL: createObjectURLMock,
  revokeObjectURL: vi.fn(),
})

const authedGet = '/ext/artifacts/files/cat.png'

describe('AttachmentImage 附件图片', () => {
  afterEach(() => vi.clearAllMocks())

  it('/uploads src 经鉴权内容端点取流渲染 blob（不透传直链）', async () => {
    getMock.mockResolvedValueOnce({ data: new Blob(['x']) })
    render(<AttachmentImage src="/uploads/cat.png" alt="cat.png" />)
    const img = await screen.findByRole('img')
    expect(img).toHaveAttribute('src', 'blob:mock-1')
    // 取流键 = /uploads url basename → artifacts_file_content（apiClient 携认证头）；
    // 走 apiClient 默认基址——移动壳同源 origin 是本地资源服务器（无 /ext 路由），
    // 置 baseURL:'' 会把取流打到 localhost 404（R4 移动端附件轮回归钉）
    expect(getMock).toHaveBeenCalledWith(
      authedGet,
      expect.objectContaining({ responseType: 'blob' }),
    )
    expect(getMock.mock.calls[0][1]?.baseURL).toBeUndefined()
    expect(img).toHaveAttribute('alt', 'cat.png')
  })

  it('非 /uploads src 透传（https 外链零行为变化，不走取流）', () => {
    render(<AttachmentImage src="https://cdn.example.com/cat.png" alt="cat.png" />)
    const img = screen.getByRole('img')
    expect(img).toHaveAttribute('src', 'https://cdn.example.com/cat.png')
    expect(getMock).not.toHaveBeenCalled()
  })

  it('取流失败（HTTP 404/401）→ 显式失败卡，不再渲染 img', async () => {
    getMock.mockRejectedValueOnce({ response: { status: 404 } })
    render(<AttachmentImage src="/uploads/gone.png" alt="gone.png" />)
    expect(await screen.findByTestId('attachment-image-failed')).toBeInTheDocument()
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText('附件加载失败')).toBeInTheDocument()
    expect(screen.getByText('gone.png')).toBeInTheDocument()
  })

  it('解码 onError（取流成功但渲染失败）→ 显式失败卡', async () => {
    getMock.mockResolvedValueOnce({ data: new Blob(['x']) })
    render(<AttachmentImage src="/uploads/cat.png" alt="cat.png" />)
    fireEvent.error(await screen.findByRole('img'))
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByTestId('attachment-image-failed')).toBeInTheDocument()
  })

  it('无 alt 时失败卡回退展示 src（来源仍可辨识）', async () => {
    getMock.mockRejectedValueOnce({ response: { status: 401 } })
    render(<AttachmentImage src="/uploads/anonymous.png" />)
    expect(await screen.findByTestId('attachment-image-failed')).toBeInTheDocument()
    expect(screen.getByText('/uploads/anonymous.png')).toBeInTheDocument()
  })

  it('src 变化后失败态自动复位（同组件渲染新附件不再残留失败卡）', async () => {
    getMock.mockResolvedValueOnce({ data: new Blob(['x']) })
    const { rerender } = render(<AttachmentImage src="/uploads/a.png" alt="a.png" />)
    fireEvent.error(await screen.findByRole('img'))
    expect(screen.getByTestId('attachment-image-failed')).toBeInTheDocument()
    getMock.mockResolvedValueOnce({ data: new Blob(['y']) })
    rerender(<AttachmentImage src="/uploads/b.png" alt="b.png" />)
    const img = await screen.findByRole('img')
    await waitFor(() => expect(img).toHaveAttribute('src', 'blob:mock-1'))
    expect(screen.queryByTestId('attachment-image-failed')).toBeNull()
  })
})
