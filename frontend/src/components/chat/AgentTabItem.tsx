/**
 * Agent Tab Item 组件
 *
 * 显示单个管道标签页：主 Tab（不可关闭）显示主管道标题（缺失时兜底 agent 名），子 Tab 显示任务标题。
 * 层级关系由管道管理面板的树形结构承载，标签栏不展示层级徽章。
 *
 * 注意：Agent 切换功能位于会话列表三点菜单（SessionList），
 *       模型名显示位于对话栏 header（ChatContainer），
 *       标签栏仅负责 Tab 导航。
 */

import { forwardRef } from 'react'
import { X } from '@/assets/icons'
import {
  BAND_BUTTON_CLASS,
  BAND_TAB_MAX_WIDTH_CLASS,
  BAND_TAB_MIN_WIDTH_CLASS,
  BAND_TAB_WIDTH_CLASS,
} from '@/components/layout/bandButton'
import { TabLabel } from '@/components/layout/TabLabel'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
import { cn } from '@/lib/utils'
import { isTabClosable } from '@/utils/tabCloseSemantics'
import { tabStatusSemantics } from './tabStatusSemantics'
import type { AgentLevel } from '@/types/models'
import type { AgentTabStatus } from '@/types/task'

/**
 * Agent Tab 数据接口
 */
export interface AgentTabItemData {
  /** Tab ID */
  id: string
  /** Agent 名称 */
  name: string
  /** Agent 层级 */
  agentLevel: AgentLevel | undefined
  /** Tab 状态 */
  status: AgentTabStatus
  /** 是否为当前激活 Tab */
  isActive: boolean
  /** 未读消息数 */
  unreadCount?: number
  /** 是否可关闭 */
  canClose: boolean
  /** 是否固定（钉住）：固定签不给单签关闭入口、不受批量关签影响 */
  isPinned?: boolean
  /** Agent 路径 */
  path?: string[]
}

export interface AgentTabItemProps {
  /** Tab 数据 */
  tab: AgentTabItemData
  /** 点击回调 */
  onClick: () => void
  /** 关闭回调 */
  onClose?: () => void
  /** 拖拽换位回调（拖拽 tab 落到本 tab 上时触发） */
  onReorder?: (dragTabId: string, targetTabId: string) => void
  /** 右键菜单入口（与工作区标签栏同一菜单状态机） */
  onContextMenu?: (e: React.MouseEvent) => void
  /** 自定义类名 */
  className?: string
}

/** 进行中的拖拽源（HTML5 DnD 的 dataTransfer 在 dragover 期不可读，用模块态中转） */
let dragSource: { id: string; path: string } | null = null

/** 获取状态图标 */
const getStatusIcon = (status: AgentTabStatus) => {
  switch (status) {
    case 'running':
      return '\u25CF'
    case 'completed':
      return '\u2713'
    case 'waiting_input':
      return '\uD83D\uDCAC'
    case 'failed':
      return '\u2715'
    case 'unknown':
      return '\uFF1F'
    default:
      return '\u25CF'
  }
}

/**
 * AgentTabItem 组件
 */
export const AgentTabItem = forwardRef<HTMLDivElement, AgentTabItemProps>(
  ({ tab, onClick, onClose, onReorder, onContextMenu, className }, ref) => {
    const isMainTab = tab.agentLevel === 1
    // 移动端语义化 chip（P1-7，MuMu 实测 shots/09「● 灵汐 0」符号含义不明）：
    // 点色（连接/任务状态）+ 状态词（可读），符号 ●✓✕ 退场；桌面维持符号位
    const isMobile = useIsMobileViewport()
    const semantic = tabStatusSemantics(tab.status)

    return (
      <div
        ref={ref}
        role="tab"
        aria-selected={tab.isActive}
        tabIndex={0}
        draggable
        onDragStart={(e) => {
          dragSource = { id: tab.id, path: tab.path?.join('/') ?? '' }
          e.dataTransfer.effectAllowed = 'move'
          e.dataTransfer.setData('text/plain', tab.id)
        }}
        onDragOver={(e) => {
          if (
            dragSource &&
            dragSource.id !== tab.id &&
            dragSource.path === (tab.path?.join('/') ?? '')
          ) {
            e.preventDefault()
          }
        }}
        onDrop={(e) => {
          e.preventDefault()
          const source = dragSource
          dragSource = null
          if (source && source.id !== tab.id && source.path === (tab.path?.join('/') ?? '')) {
            onReorder?.(source.id, tab.id)
          }
        }}
        onClick={onClick}
        onContextMenu={onContextMenu}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onClick()
          }
        }}
        className={cn(
          BAND_BUTTON_CLASS,
          'group relative overflow-hidden font-medium',
          BAND_TAB_WIDTH_CLASS,
          BAND_TAB_MIN_WIDTH_CLASS,
          BAND_TAB_MAX_WIDTH_CLASS,
          isMainTab && 'bg-primary/5 border-primary/20 border',
          tab.isActive
            ? 'bg-primary/15 text-primary border-primary/30 border shadow-sm'
            : 'hover:bg-muted text-muted-foreground hover:text-foreground border border-transparent',
          className,
        )}
        title={tab.path?.join(' \u2192 ') || tab.name}
      >
        {isMobile ? (
          <>
            {/* 状态点（连接/任务状态语义，颜色+呼吸由语义表单点承载） */}
            <span
              aria-hidden="true"
              data-testid="agent-tab-status-dot"
              className={cn(
                'h-2 w-2 flex-shrink-0 rounded-full',
                semantic.dotClass,
                semantic.pulsing && 'animate-pulse',
              )}
            />
            <TabLabel title={tab.name} className="text-sm font-medium" />
            {/* 状态词（可读语义，P1-7：●✓✕ 符号在移动端的替身） */}
            <span
              data-testid="agent-tab-status-label"
              className="text-muted-foreground flex-shrink-0 text-[10px]"
            >
              {semantic.label}
            </span>
          </>
        ) : (
          <>
            <span
              className={cn(
                'flex-shrink-0 text-xs',
                tab.status === 'running' && 'text-primary animate-pulse',
                tab.status === 'waiting_input' && 'text-warning animate-pulse',
                tab.status === 'completed' && 'text-success',
                tab.status === 'failed' && 'text-destructive',
              )}
            >
              {getStatusIcon(tab.status)}
            </span>

            <TabLabel title={tab.name} className="text-sm font-medium" />
          </>
        )}

        {tab.unreadCount && tab.unreadCount > 0 && (
          <span className="bg-warning text-warning-foreground flex-shrink-0 rounded-full px-1.5 py-0.5 text-xs font-medium">
            {tab.unreadCount > 9 ? '9+' : tab.unreadCount}
          </span>
        )}

        <span className="h-icon-md w-icon-md flex flex-shrink-0 items-center justify-center">
          {isTabClosable(tab) && onClose && (
            <button
              onClick={(e) => {
                e.stopPropagation()
                onClose()
              }}
              className={cn(
                'h-icon-md w-icon-md flex items-center justify-center rounded',
                'opacity-100 transition-opacity md:opacity-0 md:group-hover:opacity-100',
                'hover:bg-destructive/20 text-muted-foreground hover:text-destructive',
              )}
              title="关闭 Tab"
            >
              <X className="h-icon-xs w-icon-xs" />
            </button>
          )}
        </span>

        {tab.isActive && (
          <span className="bg-primary absolute right-0 bottom-0 left-0 h-0.5 rounded-full" />
        )}
      </div>
    )
  },
)

AgentTabItem.displayName = 'AgentTabItem'
