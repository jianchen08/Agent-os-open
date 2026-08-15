/**
 * 通用引用卡片组件——聊天场景中"引用了什么"的统一渲染单元。
 *
 * 可扩展性：kind → 渲染器注册表。内置默认渲染（缩略图 + 标题 + 副标题 + kind 徽章），
 * 其他引用类型（文件 / 设计稿 / 任意外部对象）经 registerReferenceRenderer 注册
 * 自定义渲染即可复用，无需改动本组件。
 *
 * 当前消费方：
 * - ChatContainer：Godot 实时选中引用（输入框上方，选中出现 / 取消消失）
 * - MessageItem：对话历史中插件注入的 <reference> 引用消息渲染
 */
import { Box } from '@/assets/icons'
import type { ReactNode } from 'react'

/** 单个引用卡片的数据契约 */
export interface ReferenceChipData {
  /** 引用类型（如 godot-node、file、image；用于渲染器分发与徽章展示） */
  kind: string
  /** 主标题（如节点名 / 文件名） */
  title: string
  /** 副标题（如类型 + 路径） */
  subtitle?: string
  /** 预览图 URL（可选） */
  previewUrl?: string
}

/** 自定义渲染器：返回卡片内容（不含外层容器） */
type ReferenceRenderer = (data: ReferenceChipData) => ReactNode

const renderers = new Map<string, ReferenceRenderer>()

/** 注册某类引用的自定义渲染器（后注册覆盖先注册） */
export function registerReferenceRenderer(kind: string, renderer: ReferenceRenderer): void {
  renderers.set(kind, renderer)
}

/** 解析插件注入的引用消息内容（<reference source="godot" scene="...">…</reference>） */
export function parseReferenceMessage(
  content: string,
): { source: string; scene: string; items: Array<{ name: string; type: string; path: string }> } | null {
  if (typeof content !== 'string' || !content.startsWith('<reference ')) return null
  const sceneMatch = content.match(/scene="([^"]*)"/)
  const items: Array<{ name: string; type: string; path: string }> = []
  for (const m of content.matchAll(/- (.+?) \((.+?)\) @ (.+)/g)) {
    items.push({ name: m[1], type: m[2], path: m[3] })
  }
  return {
    source: 'godot',
    scene: sceneMatch?.[1] ?? '',
    items,
  }
}

/** 单个引用卡片（默认渲染：可选缩略图 + 标题/副标题 + kind 徽章） */
export function ReferenceChip({ data }: { data: ReferenceChipData }) {
  const custom = renderers.get(data.kind)
  if (custom) {
    return <div className="bg-background/60 flex items-center gap-2 rounded-lg border border-border/40 px-2 py-1.5">{custom(data)}</div>
  }
  return (
    <div className="bg-background/60 flex items-center gap-2 rounded-lg border border-border/40 px-2 py-1.5">
      {data.previewUrl ? (
        <img
          src={data.previewUrl}
          alt={data.title}
          className="h-8 w-8 shrink-0 rounded object-cover"
          loading="lazy"
          onError={(e) => {
            ;(e.target as HTMLImageElement).style.display = 'none'
          }}
        />
      ) : (
        <Box className="text-muted-foreground h-icon-md w-icon-md shrink-0" />
      )}
      <div className="min-w-0">
        <div className="truncate text-xs font-medium">{data.title}</div>
        {data.subtitle && <div className="text-muted-foreground truncate text-[11px]">{data.subtitle}</div>}
      </div>
      <span className="bg-primary/10 text-primary ml-1 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium">
        {data.kind}
      </span>
    </div>
  )
}
