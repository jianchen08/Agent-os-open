/**
 * 消息操作按钮组件
 *
 * 提供消息的复制、编辑、回退（二次确认）与「重新生成」操作。
 * 所有操作入口均以回调形式暴露，调用方决定是否接入（不传回调即不显示按钮）。
 *
 * ‹i/n› 多代切换器不在本组件内：它是到达历史代版本的唯一入口，须常驻可见
 * （原藏于 hover 门控操作组内 = 用户找不到入口），已独立为 ./SegmentSwitcher，
 * 由 MessageItem 元信息行单独挂载。
 */

import { Share2 } from 'lucide-react'
import { useRef, useState, type FC, type MouseEvent as ReactMouseEvent } from 'react'
import { toast } from 'sonner'
import { Copy, MoreHorizontal, Pencil, RotateCcw, RefreshCw } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
import { captureMessageCard, copyPngToClipboard, downloadDataUrl, findMessageCard } from '@/lib/shareCard'
import { SegmentSwitcher } from './SegmentSwitcher'
import type { Message } from '@/types/models'

/**
 * 消息操作组件属性
 */
export interface MessageActionsProps {
  /** 消息对象 */
  message: Message
  /** 会话 ID */
  sessionId: string
  /** 是否为用户消息 */
  isUserMessage?: boolean
  /** 是否禁用操作 */
  disabled?: boolean
  /** 是否为最后一条消息（控制重试/编辑回复按钮显示） */
  isLastMessage?: boolean
  /** 消息复制回调 */
  onCopy?: () => void
  /** 消息编辑回调（打开内联编辑器，保存路径由 MessageItem 接管） */
  onEdit?: () => void
  /** 消息内容更新回调（用于版本切换） */
  onContentUpdate?: (content: string) => void
  /** 重新生成回调（最后一条 assistant 消息，含失败/中断态） */
  onRegenerate?: () => void
  /** 回退回调（user 消息二次确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
  /** ‹i/n› 多代切换器锚点 base_seq（消息段模型；null/缺省不显示切换器，
   *  由 MessageList 按「轮末条 assistant + 锚点多段」标定） */
  segmentSwitcherBaseSeq?: number | null
}

/** 判定消息是否处于失败/中断态（空内容时仍需保留可重试入口） */
function isFailedOrInterrupted(message: Message): boolean {
  return (
    message.status === 'error' ||
    message.status === 'failed' ||
    message.status === 'interrupted'
  )
}

/**
 * 消息操作按钮组件
 *
 * 引用/评审入口不在本组件：2026-11-05 R4 件③按用户裁定改为「选中消息文字 →
 * CitationFloatBar 浮层」形态（与工作区制品同款），挂载在 MessageItem 气泡
 * （hooks/useBubbleCitationBar），非常驻按钮+输入框。
 */
export const MessageActions: FC<MessageActionsProps> = ({
  message,
  sessionId,
  disabled = false,
  onCopy,
  onEdit,
  onRollbackTo,
  onRegenerate,
  segmentSwitcherBaseSeq,
}) => {
  /** 回退二次确认（内联展开确认条） */
  const [confirmingRollback, setConfirmingRollback] = useState(false)
  /** 是否为用户消息（决定回退/编辑入口归属） */
  const isUser = message.role === 'user'
  // 移动端动作收纳（P1-5，MuMu 实测 shots/09 每条消息一份 4 图标行）：图标组
  // 收进「⋯」菜单（触控条目 ≥44px），时间戳/分支切换器不动；桌面维持 hover
  // 显隐平铺组
  const isMobile = useIsMobileViewport()
  /** 动作组锚点（菜单路径分享卡取卡用：菜单条目无事件 currentTarget，
   *  经本 ref 所在元素向上找 [data-testid="message-item"] 卡片） */
  const actionsRef = useRef<HTMLDivElement | null>(null)
  const handleShareFromMenu = () => {
    void handleShare({
      currentTarget: actionsRef.current,
    } as unknown as ReactMouseEvent<HTMLButtonElement>)
  }
  /** 是否为最后一条 assistant 消息（重新生成入口：含失败/中断态） */
  const isAssistant = message.role === 'assistant'
  const showRegenerate =
    isAssistant && !!onRegenerate && (isFailedOrInterrupted(message) || message.status === 'completed')

  /**
   * 处理复制操作
   */
  const handleCopy = () => {
    if (onCopy) {
      onCopy()
    } else {
      navigator.clipboard.writeText(message.content)
      toast.success('已复制到剪贴板')
    }
  }

  /** 确认回退：触发回调并收起确认条 */
  const [sharing, setSharing] = useState(false)
  /** 分享卡：消息气泡 DOM → PNG（优先剪贴板，失败降级下载，不静默） */
  const handleShare = async (e: ReactMouseEvent<HTMLButtonElement>) => {
    const card = findMessageCard(e.currentTarget)
    if (!card) {
      toast.error('未找到消息卡片')
      return
    }
    setSharing(true)
    try {
      const dataUrl = await captureMessageCard(card)
      try {
        await copyPngToClipboard(dataUrl)
        toast.success('分享卡已复制到剪贴板')
      } catch {
        downloadDataUrl(dataUrl, `share-card-${message.id}.png`)
        toast.success('剪贴板不可用，已下载分享卡图片')
      }
    } catch (err) {
      toast.error(`分享卡生成失败: ${String(err)}`)
    } finally {
      setSharing(false)
    }
  }

  /** 确认回退：触发回调并收起确认条 */
  const handleConfirmRollback = () => {
    setConfirmingRollback(false)
    if (onRollbackTo) {
      onRollbackTo(message.id)
    }
  }

  /** 二次确认条（内联展开，确认前不产生任何副作用） */
  const renderRollbackConfirm = () => {
    if (!confirmingRollback) return null
    return (
      <span
        className="flex items-center gap-1 rounded-md bg-[rgb(var(--status-warning-rgb,217_119_6)/0.15)] px-1.5 py-0.5 text-xs text-[var(--status-warning)]"
        data-testid="rollback-confirm"
      >
        <span>确认回退？</span>
        <button
          type="button"
          className="font-medium underline hover:opacity-80"
          onClick={handleConfirmRollback}
          disabled={disabled}
        >
          确认
        </button>
        <button
          type="button"
          className="opacity-70 hover:opacity-100"
          onClick={() => setConfirmingRollback(false)}
          disabled={disabled}
        >
          取消
        </button>
      </span>
    )
  }

  /** 菜单条目通用触控高度（移动端 ≥44px，优化原则 4） */
  const menuItemClass = 'min-h-11'

  return (
    <>
      {isMobile ? (
        /* 移动端：单「⋯」入口（收纳后功能仍可达：复制/分享/编辑/回退/重新生成
            全部在菜单内；回退仍走内联二次确认条，菜单关闭后出现在触发器旁） */
        <div className="flex items-center gap-0.5" ref={actionsRef}>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="sm"
                className="h-11 w-11 p-0"
                disabled={disabled}
                title="消息操作"
                aria-label="消息操作"
                data-testid="message-actions-more"
              >
                <MoreHorizontal className="h-icon-md w-icon-md" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align={isUser ? 'end' : 'start'} collisionPadding={12} className="w-48">
              <DropdownMenuItem className={menuItemClass} onSelect={handleCopy} data-testid="message-menu-copy">
                <Copy className="h-icon-sm w-icon-sm" />
                复制
              </DropdownMenuItem>
              <DropdownMenuItem
                className={menuItemClass}
                onSelect={handleShareFromMenu}
                disabled={disabled || sharing}
                data-testid="message-menu-share"
              >
                <Share2 className="h-icon-sm w-icon-sm" />
                分享为图片
              </DropdownMenuItem>
              {isUser && onEdit && (
                <DropdownMenuItem className={menuItemClass} onSelect={onEdit} disabled={disabled} data-testid="message-menu-edit">
                  <Pencil className="h-icon-sm w-icon-sm" />
                  编辑并重新发送
                </DropdownMenuItem>
              )}
              {isUser && onRollbackTo && (
                <DropdownMenuItem
                  className={menuItemClass}
                  onSelect={() => setConfirmingRollback(true)}
                  disabled={disabled}
                  data-testid="message-menu-rollback"
                >
                  <RotateCcw className="h-icon-sm w-icon-sm" />
                  回退到这条消息
                </DropdownMenuItem>
              )}
              {showRegenerate && (
                <DropdownMenuItem className={menuItemClass} onSelect={() => onRegenerate?.()} disabled={disabled} data-testid="message-menu-regenerate">
                  <RefreshCw className="h-icon-sm w-icon-sm" />
                  重新生成
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
          {renderRollbackConfirm()}
        </div>
      ) : (
        <>
          {/* 次要动作组：hover 才显形（不打扰阅读） */}
          <div className="flex items-center gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={handleCopy}
              disabled={disabled}
              title="复制"
            >
              <Copy className="h-icon-xs w-icon-xs" />
            </Button>
            {/* 分享卡：消息卡片 DOM → PNG 图片（调研报告 §四A 分享卡项；html-to-image MIT） */}
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={handleShare}
              disabled={disabled || sharing}
              title="分享为图片"
              data-testid="share-card-button"
            >
              <Share2 className="h-icon-xs w-icon-xs" />
            </Button>
          {/* 用户消息操作：编辑（打开内联编辑器）+ 回退（二次确认） */}
          {isUser && onEdit && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={onEdit}
              disabled={disabled}
              title="编辑并重新发送"
            >
              <Pencil className="h-icon-xs w-icon-xs" />
            </Button>
          )}
          {isUser && onRollbackTo && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={() => setConfirmingRollback(true)}
              disabled={disabled}
              title="回退到这条消息"
            >
              <RotateCcw className="h-icon-xs w-icon-xs" />
            </Button>
          )}
          {renderRollbackConfirm()}
          {/* 重新生成：最后一条 assistant 消息（含失败/中断态） */}
          {showRegenerate && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0"
              onClick={onRegenerate}
              disabled={disabled}
              title="重新生成"
              data-testid="regenerate-button"
            >
              <RefreshCw className="h-icon-xs w-icon-xs" />
            </Button>
          )}
          </div>
        </>
      )}
      {/* ‹i/n› 分支切换器：常驻可见（不随操作组 hover 门控）——它是到达历史代
          版本的唯一入口，藏进 hover 组内等于「没有入口」（用户实证：重试多次
          仍找不到左右切换按钮） */}
      <SegmentSwitcher
        message={message}
        sessionId={sessionId}
        disabled={disabled}
        segmentSwitcherBaseSeq={segmentSwitcherBaseSeq}
      />
    </>
  )
}
