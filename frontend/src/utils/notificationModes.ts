/**
 * 通知分类渲染声明（widget 化批1-C）：通知 category → 渲染词表。
 *
 * 数据归属：human_interaction_tool 插件（谁的数据谁声明渲染）。声明走
 * manifest capabilities.tools[].ui.notification_modes（与 interaction_modes
 * 同通道 /api/v1/schema tools[] 原样透传），GrowthLoop 装载进本注册表。
 *
 * 双路由（对齐 interactionModes 槽位架构）：插件声明覆盖内置默认件；
 * 未声明的未知分类用通用兜底（bell 状态图标 + message）；数据形状增强
 * （带 message/actions 载荷自动补对应特性，保证与现状渲染等价）——
 * 插件新增通知分类前端零改动可渲染。
 *
 * 与 interactionModes 的差异（保持现状等价）：
 * progress 特性跟分类声明走，不随 progress 数据自动补——现状只有
 * progress 分类出进度条，声明新分类需显式带上 'progress' 特性。
 */
import type { NotificationItem } from '@/types/notification'

/** 通知分类渲染词表（渲染词表——前端消费，插件声明选择） */
export type NotificationFeature =
  | 'status' // 分类状态图标（图标键 decl.icon；priority 可覆盖）
  | 'message' // message markdown 展示（有 message 载荷自动补）
  | 'progress' // 进度条 + 图标旋转动效（progress 载荷；随分类声明，不随数据自动补）
  | 'actions' // 动作按钮组（有 actions 载荷自动补）

export interface NotificationModeDecl {
  category: string
  features: NotificationFeature[]
  /** 状态图标键（前端内置图标原语；缺省 'bell' 通用图标） */
  icon?: string
}

/**
 * 内置默认件（五分类兼容层）：插件未声明时兜底，声明后可覆盖。
 * 与 human_interaction_tool 的 ui.notification_modes 声明保持同构。
 */
const DEFAULT_CATEGORY_DECLS: Record<string, NotificationModeDecl> = {
  progress: { category: 'progress', features: ['status', 'message', 'progress'], icon: 'loader' },
  alert: { category: 'alert', features: ['status', 'message', 'actions'], icon: 'alert-triangle' },
  info: { category: 'info', features: ['status', 'message'], icon: 'info' },
  success: { category: 'success', features: ['status', 'message'], icon: 'check-circle' },
  error: { category: 'error', features: ['status', 'message', 'actions'], icon: 'alert-circle' },
}

/** 未知分类兜底：通用状态图标 + 消息展示 */
const GENERIC_FALLBACK: NotificationModeDecl = {
  category: '',
  features: ['status', 'message'],
  icon: 'bell',
}

const KNOWN_FEATURES: readonly string[] = ['status', 'message', 'progress', 'actions']

const categoryDeclarations = new Map<string, NotificationModeDecl>()

/** 从 schema.tools[].ui.notification_modes 装载声明（幂等：先清空再装） */
export function loadNotificationModes(
  tools: Array<{ ui?: { notification_modes?: unknown } }>,
): void {
  categoryDeclarations.clear()
  for (const t of tools) {
    const decls = t.ui?.notification_modes
    if (!Array.isArray(decls)) continue
    for (const raw of decls) {
      if (!raw || typeof raw !== 'object') continue
      const decl = raw as { category?: unknown; features?: unknown; icon?: unknown }
      if (typeof decl.category !== 'string' || decl.category === '') continue
      if (!Array.isArray(decl.features)) continue
      const features = decl.features.filter(
        (f): f is NotificationFeature =>
          typeof f === 'string' && KNOWN_FEATURES.includes(f),
      )
      categoryDeclarations.set(decl.category, {
        category: decl.category,
        features,
        icon: typeof decl.icon === 'string' ? decl.icon : undefined,
      })
    }
  }
}

/** 按分类查声明（内置默认件兜底） */
export function getNotificationModeDecl(category: string): NotificationModeDecl | undefined {
  return categoryDeclarations.get(category) ?? DEFAULT_CATEGORY_DECLS[category]
}

/** 清空声明注册表（测试用） */
export function clearNotificationModes(): void {
  categoryDeclarations.clear()
}

export interface NotificationLayout {
  features: Set<NotificationFeature>
  iconKey: string
}

/**
 * 解析一条通知的布局（特性集 + 状态图标键）：声明/内置默认 → 通用兜底；
 * 数据形状增强（带 message/actions 载荷且特性未含 → 补上，与现状渲染等价）。
 */
export function resolveNotificationLayout(notification: NotificationItem): NotificationLayout {
  const decl =
    categoryDeclarations.get(notification.category) ??
    DEFAULT_CATEGORY_DECLS[notification.category] ??
    GENERIC_FALLBACK
  const features = new Set<NotificationFeature>(decl.features)
  if (notification.message) features.add('message')
  if (notification.actions && notification.actions.length > 0) features.add('actions')
  return { features, iconKey: decl.icon ?? 'bell' }
}