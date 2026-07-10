/**
 * 图片优化工具函数
 *
 * 提供图片格式转换、尺寸优化、WebP 支持检测等功能
 */

/**
 * 检查浏览器是否支持 WebP 格式
 */
let webpSupported: boolean | null = null

export function supportsWebP(): boolean {
  if (webpSupported !== null) {
    return webpSupported
  }

  if (typeof window === 'undefined') {
    webpSupported = false
    return false
  }

  const canvas = document.createElement('canvas')
  canvas.width = 1
  canvas.height = 1

  try {
    const dataUrl = canvas.toDataURL('image/webp')
    webpSupported = dataUrl.indexOf('data:image/webp') === 0
  } catch {
    webpSupported = false
  }

  return webpSupported
}

/**
 * 获取优化后的图片 URL
 *
 * @param url - 原始图片 URL
 * @param width - 目标宽度
 * @param quality - 图片质量 (0-100)
 * @param format - 图片格式 (webp, jpeg, png)
 * @returns 优化后的图片 URL
 *
 * @example
 * ```ts
 * const optimizedUrl = getOptimizedImageUrl('/avatar.png', 200, 80)
 * // => '/avatar.png?w=200&q=80&format=webp'
 * ```
 */
export function getOptimizedImageUrl(
  url: string,
  width: number,
  quality: number = 80,
  format?: 'webp' | 'jpeg' | 'png',
): string {
  // 如果是 data URL 或 blob URL，直接返回
  if (url.startsWith('data:') || url.startsWith('blob:')) {
    return url
  }

  // 检查是否支持 WebP
  const useWebP = format === 'webp' || (!format && supportsWebP())

  // 构建查询参数
  const params = new URLSearchParams()
  params.append('w', width.toString())
  params.append('q', quality.toString())

  if (useWebP) {
    params.append('format', 'webp')
  } else if (format) {
    params.append('format', format)
  }

  // 检查 URL 是否已经有查询参数
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}${params.toString()}`
}

/**
 * 根据设备像素比优化图片尺寸
 *
 * @param baseSize - 基础尺寸
 * @returns 考虑 DPR 后的尺寸
 */
export function getDPROptimizedSize(baseSize: number): number {
  if (typeof window === 'undefined') {
    return baseSize
  }

  const dpr = window.devicePixelRatio || 1
  // 限制最大 DPR 为 2，避免加载过大的图片
  const maxDPR = 2
  return Math.ceil(baseSize * Math.min(dpr, maxDPR))
}

/**
 * 生成响应式图片 srcset
 *
 * @param url - 原始图片 URL
 * @param sizes - 尺寸数组，如 [320, 640, 1280]
 * @param quality - 图片质量
 * @returns srcset 字符串
 *
 * @example
 * ```ts
 * const srcset = generateSrcSet('/image.jpg', [320, 640, 1280])
 * // => '/image.jpg?w=320&q=80 320w, /image.jpg?w=640&q=80 640w, ...'
 * ```
 */
export function generateSrcSet(url: string, sizes: number[], quality: number = 80): string {
  return sizes
    .map((size) => {
      const optimizedUrl = getOptimizedImageUrl(url, size, quality)
      return `${optimizedUrl} ${size}w`
    })
    .join(', ')
}

/**
 * 生成响应式图片 sizes 属性
 *
 * @param breakpoints - 断点配置
 * @returns sizes 字符串
 *
 * @example
 * ```ts
 * const sizes = generateSizes({
 *   mobile: '100vw',
 *   tablet: '50vw',
 *   desktop: '33vw',
 * })
 * // => '(max-width: 768px) 100vw, (max-width: 1024px) 50vw, 33vw'
 * ```
 */
export interface SizesConfig {
  mobile?: string
  tablet?: string
  desktop?: string
}

export function generateSizes(config: SizesConfig = {}): string {
  const sizes: string[] = []

  if (config.mobile) {
    sizes.push(`(max-width: 768px) ${config.mobile}`)
  }

  if (config.tablet) {
    sizes.push(`(max-width: 1024px) ${config.tablet}`)
  }

  if (config.desktop) {
    sizes.push(config.desktop)
  }

  return sizes.join(', ') || '100vw'
}

/**
 * 预加载图片
 *
 * @param url - 图片 URL
 * @returns Promise，当图片加载完成时 resolve
 */
export function preloadImage(url: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (typeof window === 'undefined') {
      resolve()
      return
    }

    const img = new Image()
    img.onload = () => resolve()
    img.onerror = reject
    img.src = url
  })
}

/**
 * 批量预加载图片
 *
 * @param urls - 图片 URL 数组
 * @param concurrency - 并发加载数量
 * @returns Promise，当所有图片加载完成时 resolve
 */
export async function preloadImages(urls: string[], concurrency: number = 3): Promise<void> {
  const batches: string[][] = []

  // 分批
  for (let i = 0; i < urls.length; i += concurrency) {
    batches.push(urls.slice(i, i + concurrency))
  }

  // 顺序加载每批
  for (const batch of batches) {
    await Promise.all(batch.map((url) => preloadImage(url)))
  }
}

/**
 * 计算图片的长宽比
 *
 * @param width - 图片宽度
 * @param height - 图片高度
 * @returns 长宽比字符串（用于 CSS aspect-ratio）
 */
export function getAspectRatio(width: number, height: number): string {
  return `${width} / ${height}`
}

/**
 * 根据容器宽度和长宽比计算图片高度
 *
 * @param containerWidth - 容器宽度
 * @param aspectRatio - 长宽比 (width / height)
 * @returns 计算后的高度
 */
export function calculateHeight(containerWidth: number, aspectRatio: number): number {
  return containerWidth / aspectRatio
}
