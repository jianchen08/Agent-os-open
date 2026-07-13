import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 头像组件
 * 支持图片、文字回退和状态指示器
 */

const Avatar = React.forwardRef<HTMLSpanElement, React.HTMLAttributes<HTMLSpanElement>>(
  ({ className, ...props }, ref) => (
    <span
      ref={ref}
      className={cn('relative flex h-10 w-10 shrink-0 overflow-hidden rounded-full', className)}
      {...props}
    />
  ),
)
Avatar.displayName = 'Avatar'

const AvatarImage = React.forwardRef<HTMLImageElement, React.ImgHTMLAttributes<HTMLImageElement>>(
  ({ className, src, alt, ...props }, ref) => {
    const [hasError, setHasError] = React.useState(false)

    // 当 src 变化时重置错误状态
    React.useEffect(() => {
      setHasError(false)
    }, [src])

    if (hasError || !src) {
      return null
    }

    return (
      <img
        ref={ref}
        src={src}
        alt={alt}
        className={cn('aspect-square h-full w-full object-cover', className)}
        onError={() => setHasError(true)}
        {...props}
      />
    )
  },
)
AvatarImage.displayName = 'AvatarImage'

const AvatarFallback = React.forwardRef<HTMLSpanElement, React.HTMLAttributes<HTMLSpanElement>>(
  ({ className, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(
        'bg-muted flex h-full w-full items-center justify-center rounded-full text-sm font-medium',
        className,
      )}
      {...props}
    />
  ),
)
AvatarFallback.displayName = 'AvatarFallback'

export { Avatar, AvatarImage, AvatarFallback }
