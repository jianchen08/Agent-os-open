// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * 消息内嵌图片懒解码契约测试（renderer 内存优化：位图解码是大图消息内存大户）
 *
 * 契约：
 * - markdown 附件图片（AttachmentImage）以 loading=lazy + decoding=async 渲染：
 *   视口外不加载、解码不阻塞主线程；
 * - 工具活动图片块（DetailBlock contentType='image'）列表内缩略图 decoding=async。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AttachmentImage } from '@/components/shared/markdown/AttachmentImage'
import { DetailBlock } from '../ActivityBlockViews'

// @lobehub/ui barrel 在 vitest 下无法解析（全仓既有测试一律 mock 该库）——
// 以最小 <img> 替身透传懒加载/解码属性，承担渲染契约断言。
vi.mock('@lobehub/ui', () => ({
  Image: ({
    src,
    alt,
    loading,
    decoding,
    onError,
  }: {
    src?: string
    alt?: string
    loading?: string
    decoding?: string
    onError?: (e: Event) => void
  }) => (
    <img
      src={src}
      alt={alt}
      loading={loading}
      decoding={decoding}
      onError={onError}
      data-testid="lobehub-image"
    />
  ),
}))

describe('消息内嵌图片懒解码', () => {
  it('markdown 附件图片：loading=lazy + decoding=async', () => {
    render(<AttachmentImage src="/uploads/cat.png" alt="cat.png" />)
    const img = screen.getByTestId('lobehub-image')
    expect(img).toHaveAttribute('loading', 'lazy')
    expect(img).toHaveAttribute('decoding', 'async')
  })

  it('工具活动图片块列表缩略图：decoding=async（loading=lazy 为既有契约不回退）', () => {
    render(
      <DetailBlock block={{ label: '截图', content: '/uploads/shot.png', contentType: 'image' }} />,
    )
    const img = screen.getByRole('img', { name: '预览图' })
    expect(img).toHaveAttribute('loading', 'lazy')
    expect(img).toHaveAttribute('decoding', 'async')
  })
})
