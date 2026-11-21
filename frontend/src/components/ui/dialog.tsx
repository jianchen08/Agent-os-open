import * as DialogPrimitive from '@radix-ui/react-dialog'
import * as React from 'react'
import { XIcon } from '@/assets/icons'
import { cn } from '@/lib/utils'

const Dialog = DialogPrimitive.Root

const DialogTrigger = DialogPrimitive.Trigger

const DialogPortal = DialogPrimitive.Portal

const DialogClose = DialogPrimitive.Close

const DialogOverlay = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, style, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 fixed inset-0 z-50',
      className,
    )}
    style={{
      backgroundColor: `var(--dialog-overlay-bg)`,
      opacity: `var(--dialog-overlay-opacity, 0.8)`,
      ...style,
    }}
    {...props}
  />
))
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName

const DialogContent = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    width?: string
    /**
     * 弹窗层级（内容与遮罩同层，默认 50）。
     * 审批卡详情窗挂在 GlobalInteractionOverlay（z-[10000]）之上触发：
     * 默认 50 会被底部交互层盖住——点选项只看到背景变暗、弹窗不可见，
     * 故调用方可传入高于交互层的值（如 10001）。
     */
    zIndex?: number
  }
>(({ className, children, width, zIndex = 50, ...props }, ref) => (
  <DialogPortal>
    <DialogOverlay style={{ zIndex }} />
    <DialogPrimitive.Content
      ref={ref}
      onPointerDownOutside={(e) => e.preventDefault()}
      onInteractOutside={(e) => e.preventDefault()}
      style={{
        position: 'fixed',
        left: '50%',
        top: '50%',
        transform: 'translate(-50%, -50%)',
        width: width ?? '500px',
        maxWidth: '90vw',
        zIndex,
        backgroundColor: 'hsl(var(--popover))',
        borderRadius: 'var(--dialog-radius, 1rem)',
        boxShadow: 'var(--dialog-shadow)',
        border: 'var(--dialog-border)',
      }}
      className={cn(
        'data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 flex max-h-[var(--dialog-max-h,90vh)] flex-col overflow-y-auto duration-200',
        className,
      )}
      {...props}
    >
      {children}
    </DialogPrimitive.Content>
  </DialogPortal>
))
DialogContent.displayName = DialogPrimitive.Content.displayName

const DialogHeader = ({
  className,
  children,
  showClose = true,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { showClose?: boolean }) => (
  <div
    className={cn(
      // sticky 顶沿：内容超高滚动时标题/关闭钮常驻（弹窗根是滚动容器，
      // 背景取弹窗同色遮挡滚过的内容；内容不超高时 sticky 不生效、观感不变）
      'bg-popover sticky top-0 z-10 flex flex-shrink-0 items-center justify-between gap-2 rounded-t-[inherit] p-6 pb-0',
      className,
    )}
    {...props}
  >
    <div className="flex min-w-0 flex-1 flex-col space-y-1.5 text-center sm:text-left">
      {children}
    </div>
    {showClose && (
      <DialogPrimitive.Close className="ring-offset-background focus:ring-ring data-[state=open]:bg-accent data-[state=open]:text-muted-foreground hover:bg-muted flex-shrink-0 rounded-md rounded-sm p-1 opacity-70 transition-opacity hover:opacity-100 focus:ring-2 focus:ring-offset-2 focus:outline-none disabled:pointer-events-none">
        <XIcon className="h-4 w-4" />
        <span className="sr-only">Close</span>
      </DialogPrimitive.Close>
    )}
  </div>
)
DialogHeader.displayName = 'DialogHeader'

const DialogFooter = ({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      // sticky 底沿：主操作（创建/保存等确认键）常驻视口内可达——移动端
      // 窄视口超高弹窗不再把确认键裁到滚动区末尾（R9 巡检 #5）；不超高时
      // sticky 不生效、观感不变。border-t 兼作钉住态与滚动内容分隔线。
      'bg-popover sticky bottom-0 z-10 flex flex-shrink-0 flex-col-reverse rounded-b-[inherit] border-t p-6 pt-4 sm:flex-row sm:justify-end sm:space-x-2',
      className,
    )}
    {...props}
  />
)
DialogFooter.displayName = 'DialogFooter'

const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title
    ref={ref}
    className={cn('text-lg leading-none font-semibold tracking-tight break-words', className)}
    {...props}
  />
))
DialogTitle.displayName = DialogPrimitive.Title.displayName

const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description
    ref={ref}
    className={cn('text-muted-foreground text-sm break-words', className)}
    {...props}
  />
))
DialogDescription.displayName = DialogPrimitive.Description.displayName

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogTrigger,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
}
