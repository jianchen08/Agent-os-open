/**
 * 输入框引用卡片行（2026-11-05 R5 卡片化；R6 容器内形态）。
 *
 * 「引用/评审」动作入列的结构化条目（chatInputStore.citationsByDraftKey）在输入
 * 面的显示形态：紧凑单行卡片——kind 图标+徽章色 + 摘录前缀省略（CSS truncate）
 * + 评语文本后置（muted 色系、自身截断、hover 全文；无独立标记徽章——文本可见
 * 即标记本身）+ × 移除。卡片只是输入面显示形态，发送产物不变（发送装配期经
 * citationCompose 拼回现有 markdown 块引用格式）。
 *
 * 与消息流 ReferenceChip 是两条通道（实时选中镜像 vs 已定格入列条目），语义不同
 * 不做物理合并，仅风格对齐；kind 判定真源在 utils/citationKind（输入框紧凑版
 * 独立注册表，未注册 kind 回落 artifact 档）。
 *
 * 容器内形态（R6）：本行挂在宿主输入框 bordered 容器内部（ChatInput 输入容器 /
 * FullscreenComposer 正文输入容器）顶部，行自带底部分隔线与正文输入分隔；
 * className 透传宿主预留位（ChatInput 用 pr-10 让出右上角展开/收起悬浮钮，
 * 与 textarea 自身的 pr-10 同款预留，保证 × 可点）。
 *
 * 截断预算（R6 登记）：评语 max-w-[40%] shrink-0（自身截断封顶卡片四成宽），
 * 摘录 flex-1 min-w-0 吃剩余全部宽（先压摘录后压评语的视觉优先级）。
 *
 * 显示层截短（R7 Bug③，R8 用户裁定收紧至前几个字符量级）：摘录渲染截短至
 * CARD_EXCERPT_DISPLAY_MAX_CHARS（16 字符）+省略号，摘录与评语单行截断
 * （truncate+whitespace-nowrap，不换行撑高卡片）——存储层快照（store 入口
 * 400 字截断）不变，发送产物契约零改动。
 *
 * 多条折叠策略：超高滚动（overflow-y-auto + 封高类 scrollMaxClass——默认
 * max-h-24=主输入框侧登记值；全屏浮窗实例 max-h-32 防长卡片撑破容器，R7 Bug②；
 * 选简做——任务书两案取其一，登记于台账 R5）。
 */

import { Box, FileCode, FileText, Image as ImageIcon, MessageSquare, X } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { useChatInputStore, type CitationItem } from '@/stores/chatInputStore'
import type { LucideIcon } from '@/assets/icons'
import type { CitationKind } from '@/utils/citationKind'

/** kind → 视觉元数据（图标 + 徽章色档 + 标签；色档均取仓内已验证 WCAG 配对） */
interface CitationKindMeta {
  icon: LucideIcon
  /** 徽章色档（bg/fg 成对，component-color-pairs 门禁同源约束） */
  badgeClass: string
  label: string
}

const KIND_META: Record<CitationKind, CitationKindMeta> = {
  code: { icon: FileCode, badgeClass: 'bg-primary/10 text-primary', label: '代码' },
  doc: { icon: FileText, badgeClass: 'bg-status-info/10 text-status-info', label: '文档' },
  image: { icon: ImageIcon, badgeClass: 'bg-status-success/10 text-status-success', label: '图片' },
  message: { icon: MessageSquare, badgeClass: 'bg-status-warning/10 text-status-warning', label: '消息' },
  artifact: { icon: Box, badgeClass: 'bg-accent/10 text-muted-foreground', label: '制品' },
}

/** 未注册 kind 的回落档（扩展缝：新 kind 在 KIND_META 登记视觉） */
const FALLBACK_META = KIND_META.artifact

/**
 * 卡片摘录显示层截短阈值（R7 Bug③，约 60 字符）：显示截短只在渲染层做，
 * 存储层快照（chatInputStore 入口 400 字截断）不变——发送装配读快照，
 * 发送产物契约零改动。
 */
export const CARD_EXCERPT_DISPLAY_MAX_CHARS = 16

/** 显示层截短：超阈值取前 N 字符加省略号（≤阈值原样显示） */
function displayExcerpt(excerpt: string): string {
  if (excerpt.length <= CARD_EXCERPT_DISPLAY_MAX_CHARS) return excerpt
  return `${excerpt.slice(0, CARD_EXCERPT_DISPLAY_MAX_CHARS)}…`
}

function kindMeta(kind: string): CitationKindMeta {
  return (KIND_META as Record<string, CitationKindMeta | undefined>)[kind] ?? FALLBACK_META
}

/** 单张紧凑卡片（单行：图标+徽章+摘录+评语标记+×） */
function CitationCard({
  item,
  onRemove,
}: {
  item: CitationItem
  onRemove: () => void
}) {
  const meta = kindMeta(item.kind)
  const Icon = meta.icon
  return (
    <div
      data-testid="citation-card"
      data-card-id={item.id}
      data-kind={item.kind}
      className={cn(
        'border-border/40 bg-background/60 flex min-w-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs',
      )}
      title={`${meta.label} · ${item.title}${item.comment ? ` · ${item.comment}` : ''}`}
    >
      <Icon className="text-muted-foreground h-icon-sm w-icon-sm shrink-0" />
      <span
        data-testid="citation-card-kind-badge"
        className={cn('shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium', meta.badgeClass)}
      >
        {meta.label}
      </span>
      <span className="text-muted-foreground shrink-0 max-w-32 truncate">{item.title}</span>
      <span
        data-testid="citation-card-excerpt"
        className="min-w-0 flex-1 truncate whitespace-nowrap"
      >
        {displayExcerpt(item.excerpt)}
      </span>
      {item.comment && (
        <span
          data-testid="citation-card-comment"
          className="text-muted-foreground border-border/40 max-w-[40%] shrink-0 truncate whitespace-nowrap border-l pl-1.5"
          title={item.comment}
        >
          {item.comment}
        </span>
      )}
      <button
        type="button"
        onClick={onRemove}
        className={cn(
          'text-muted-foreground hover:bg-destructive/20 hover:text-destructive',
          'flex h-icon-md w-icon-md shrink-0 items-center justify-center rounded',
        )}
        title="移除引用"
        aria-label={`移除引用 ${item.title}`}
        data-testid="citation-card-remove"
      >
        <X className="h-icon-xs w-icon-xs" />
      </button>
    </div>
  )
}

/**
 * 引用卡片行（draftKey 定位同键域引用列；空列不渲染；className 透传宿主预留位）。
 * scrollMaxClass（R7 Bug②）：行自身封高类（多条折叠超高滚动）——默认
 * max-h-24（主输入框侧登记值），全屏浮窗实例传 max-h-32（长卡片限高防顶出）。
 */
export function CitationCardRow({
  draftKey,
  className,
  scrollMaxClass = 'max-h-24',
}: {
  draftKey?: string
  className?: string
  /** 行自身封高类（twMerge 覆盖默认 max-h-24） */
  scrollMaxClass?: string
}) {
  const citations = useChatInputStore((s) =>
    draftKey ? s.citationsByDraftKey[draftKey] : undefined,
  )
  const removeCitation = useChatInputStore((s) => s.removeCitation)

  if (!draftKey || !citations || citations.length === 0) return null

  return (
    <div
      data-testid="citation-card-row"
      className={cn('border-border/40 overflow-y-auto border-b', scrollMaxClass, className)}
    >
      <div className="flex flex-col gap-1 px-2 py-1.5">
        {citations.map((item) => (
          <CitationCard
            key={item.id}
            item={item}
            onRemove={() => removeCitation(draftKey, item.id)}
          />
        ))}
      </div>
    </div>
  )
}
