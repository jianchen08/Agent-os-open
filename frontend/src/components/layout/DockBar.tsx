/**
 * Dock 栏组件
 *
 * 动态显示模块图标和状态指示灯
 */

import React from 'react'
import type { DockItem } from '@/types/layout'

/** Dock 栏属性 */
interface DockBarProps {
  /** Dock 图标项列表 */
  items: DockItem[]
  /** 图标尺寸 */
  iconSize?: number
  /** 图标间距 */
  iconGap?: number
}

/**
 * Dock 栏组件
 *
 * 渲染模块图标按钮，支持圆点指示灯和徽章计数
 */
export function DockBar({ items, iconSize = 20, iconGap = 6 }: DockBarProps) {
  if (items.length === 0) {
    return <div className="text-xs text-muted-foreground">Dock 栏为空</div>
  }

  return (
    <div className="flex items-center" style={{ gap: iconGap }}>
      {items.map(item => (
        <button
          key={item.id}
          className={`relative flex items-center justify-center rounded-md transition-colors ${
            item.isActive ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50 text-muted-foreground'
          }`}
          style={{ width: iconSize + 12, height: iconSize + 12 }}
          onClick={item.onClick}
          title={item.label}
        >
          <span style={{ fontSize: iconSize }}>{item.icon}</span>

          {/* 状态指示灯 */}
          {item.indicator === 'dot' && (
            <span
              className="absolute -top-0.5 -right-0.5 rounded-full"
              style={{
                width: 6,
                height: 6,
                backgroundColor: item.indicatorColor || 'var(--primary)',
              }}
            />
          )}
          {item.indicator === 'badge' && item.badgeCount !== undefined && item.badgeCount > 0 && (
            <span className="absolute -top-1 -right-1 bg-destructive text-destructive-foreground text-[10px] rounded-full min-w-[16px] h-4 flex items-center justify-center px-1">
              {item.badgeCount > 99 ? '99+' : item.badgeCount}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
