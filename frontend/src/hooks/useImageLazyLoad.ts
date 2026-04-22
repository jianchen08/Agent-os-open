/**
 * 图片懒加载 Hook
 *
 * 使用 Intersection Observer API 实现图片懒加载
 * 当图片进入视口时才加载，提升初始页面加载性能
 */

import { useEffect, useState, useRef } from 'react'

export interface UseImageLazyLoadOptions {
  /** 根边距，用于提前加载图片 */
  rootMargin?: string
  /** 是否只加载一次 */
  once?: boolean
}

export interface UseImageLazyLoadResult {
  /** 图片 ref */
  imgRef: React.RefObject<HTMLImageElement | null>
  /** 图片源 URL（加载时设置） */
  imageSrc: string | undefined
  /** 是否正在加载 */
  isLoading: boolean
  /** 是否加载失败 */
  isError: boolean
}

/**
 * 图片懒加载 Hook
 *
 * @param src - 图片 URL
 * @param options - 配置选项
 * @returns 图片 ref、加载状态和图片源
 */
export function useImageLazyLoad(
  src: string,
  options: UseImageLazyLoadOptions = {}
): UseImageLazyLoadResult {
  const { rootMargin = '100px', once = true } = options

  const [imageSrc, setImageSrc] = useState<string>()
  const [isLoading, setIsLoading] = useState(false)
  const [isError, setIsError] = useState(false)
  const imgRef = useRef<HTMLImageElement>(null)

  useEffect(() => {
    // 检查浏览器是否支持 Intersection Observer
    if (typeof window === 'undefined' || !window.IntersectionObserver) {
      // 不支持则直接加载
      setImageSrc(src)
      return
    }

    const img = imgRef.current
    if (!img) return

    // 创建 Intersection Observer
    const observer = new IntersectionObserver(
      entries => {
        entries.forEach(entry => {
          // 当图片进入视口时
          if (entry.isIntersecting) {
            setImageSrc(src)
            setIsLoading(true)

            // 如果只加载一次，取消观察
            if (once) {
              observer.disconnect()
            }
          }
        })
      },
      {
        rootMargin,
        threshold: 0.01,
      }
    )

    // 开始观察图片
    observer.observe(img)

    // 清理函数
    return () => {
      observer.disconnect()
    }
  }, [src, rootMargin, once])

  // 监听图片加载事件
  useEffect(() => {
    const img = imgRef.current
    if (!img || !imageSrc) return

    const handleLoad = () => {
      setIsLoading(false)
    }

    const handleError = () => {
      setIsLoading(false)
      setIsError(true)
    }

    img.addEventListener('load', handleLoad)
    img.addEventListener('error', handleError)

    return () => {
      img.removeEventListener('load', handleLoad)
      img.removeEventListener('error', handleError)
    }
  }, [imageSrc])

  return {
    imgRef,
    imageSrc,
    isLoading,
    isError,
  }
}

/**
 * 批量图片懒加载 Hook
 *
 * @param urls - 图片 URL 数组
 * @param options - 配置选项
 * @returns 图片 ref 数组、加载状态和图片源数组
 */
export function useBatchImageLazyLoad(
  urls: string[],
  options: UseImageLazyLoadOptions = {}
) {
  const [imageSrcs, setImageSrcs] = useState<(string | undefined)[]>(
    new Array(urls.length).fill(undefined)
  )
  const refs = useRef<(HTMLImageElement | null)[]>([])

  useEffect(() => {
    if (typeof window === 'undefined' || !window.IntersectionObserver) {
      // 不支持则直接加载所有图片
      setImageSrcs(urls)
      return
    }

    const { rootMargin = '100px', once = true } = options

    // 创建 Intersection Observer
    const observer = new IntersectionObserver(
      entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            const index = Number((entry.target as HTMLImageElement).dataset.index)
            setImageSrcs(prev => {
              const newSrcs = [...prev]
              newSrcs[index] = urls[index]
              return newSrcs
            })

            if (once) {
              observer.unobserve(entry.target)
            }
          }
        })
      },
      {
        rootMargin,
        threshold: 0.01,
      }
    )

    // 观察所有图片元素
    refs.current.forEach((img, index) => {
      if (img) {
        img.dataset.index = index.toString()
        observer.observe(img)
      }
    })

    return () => {
      observer.disconnect()
    }
  }, [urls, options])

  return {
    refs,
    imageSrcs,
  }
}

/**
 * 背景图片懒加载 Hook
 *
 * @param src - 背景图片 URL
 * @param options - 配置选项
 * @returns 包含 ref 和 style 的对象
 */
export function useBackgroundImageLazyLoad(
  src: string,
  options: UseImageLazyLoadOptions = {}
) {
  const [backgroundImage, setBackgroundImage] = useState<string>()
  const elementRef = useRef<HTMLDivElement>(null)
  const { rootMargin = '100px', once = true } = options

  useEffect(() => {
    if (typeof window === 'undefined' || !window.IntersectionObserver) {
      setBackgroundImage(`url(${src})`)
      return
    }

    const element = elementRef.current
    if (!element) return

    const observer = new IntersectionObserver(
      entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            setBackgroundImage(`url(${src})`)

            if (once) {
              observer.disconnect()
            }
          }
        })
      },
      {
        rootMargin,
        threshold: 0.01,
      }
    )

    observer.observe(element)

    return () => {
      observer.disconnect()
    }
  }, [src, rootMargin, once])

  return {
    ref: elementRef,
    style: backgroundImage ? { backgroundImage } : undefined,
  }
}
