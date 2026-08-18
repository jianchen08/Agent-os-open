/**
 * 制品预览组件
 *
 * 根据 ui_schema（type: "artifact_preview"）渲染单个制品的预览，
 * 按制品类型（text/image/code/document/data）选择合适的展示方式。
 *
 * @module ArtifactPreviewWidget
 */

import React from 'react'
import { FileText, Image as ImageIcon, FileCode, File, Database } from '@/assets/icons'
import { CodeBlock, MarkdownRenderer } from '@/components/shared/markdown'

/** 制品类型 */
type ArtifactKind = 'text' | 'image' | 'code' | 'document' | 'data' | 'composite' | 'file'

/** 制品预览项（与 ui_schema.props.artifact 结构对齐） */
interface PreviewArtifact {
  /** 制品 ID */
  id: string
  /** 制品标题 */
  title?: string
  /** 制品类型 */
  type?: ArtifactKind
  /** 制品内容（文本类制品为正文；图片类为 URL） */
  content?: string
  /** 制品语言（code 类制品用于语法提示，仅展示） */
  language?: string
}

/** 类型 → 图标映射 */
const KIND_ICON: Record<ArtifactKind, React.ReactNode> = {
  text: <FileText className="h-4 w-4" />,
  image: <ImageIcon className="h-4 w-4" />,
  code: <FileCode className="h-4 w-4" />,
  document: <FileText className="h-4 w-4" />,
  data: <Database className="h-4 w-4" />,
  composite: <File className="h-4 w-4" />,
  file: <File className="h-4 w-4" />,
}

/**
 * 规范化制品类型
 *
 * 容忍未知值，统一归入 'file'。
 *
 * @param raw - 原始类型值
 * @returns 规范化后的类型
 */
function normalizeKind(raw: unknown): ArtifactKind {
  if (typeof raw !== 'string') return 'file'
  const allowed: ArtifactKind[] = ['text', 'image', 'code', 'document', 'data', 'composite', 'file']
  return (allowed as string[]).includes(raw) ? (raw as ArtifactKind) : 'file'
}

/**
 * 从 props 安全提取制品
 *
 * 支持两种传入形式：
 * - props.artifact（单个制品对象）
 * - props（顶层 props 本身即为制品）
 *
 * @param props - 组件属性
 * @returns 制品对象（缺失时返回 null）
 */
function extractArtifact(props: Record<string, unknown>): PreviewArtifact | null {
  const candidate = (props.artifact && typeof props.artifact === 'object'
    ? props.artifact
    : props) as Record<string, unknown>
  if (typeof candidate.id !== 'string') return null
  return {
    id: candidate.id,
    title: candidate.title as string | undefined,
    type: normalizeKind(candidate.type),
    content: candidate.content as string | undefined,
    language: candidate.language as string | undefined,
  }
}

/**
 * 制品预览组件
 *
 * @param props - 组件属性
 *   - artifact: 制品对象（含 id/title/type/content/language）
 * @returns 制品预览渲染结果
 */
export function ArtifactPreviewWidget(props: Record<string, unknown>) {
  const artifact = extractArtifact(props)

  if (!artifact) {
    return (
      <div className="text-muted-foreground rounded-lg border bg-background p-6 text-center text-sm">
        暂无可预览的制品
      </div>
    )
  }

  const kind = artifact.type ?? 'file'
  const isImage = kind === 'image'

  return (
    <div className="rounded-lg border bg-background">
      {/* 标题栏 */}
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <span className="text-primary">{KIND_ICON[kind]}</span>
        <h4 className="text-foreground min-w-0 flex-1 truncate text-sm font-semibold">
          {artifact.title ?? artifact.id}
        </h4>
        <span className="text-muted-foreground rounded-full bg-muted px-2 py-0.5 text-xs">
          {kind}
        </span>
      </div>

      {/* 内容区 */}
      <div className="max-h-[480px] overflow-auto p-4">
        {isImage ? (
          artifact.content ? (
            <img
              src={artifact.content}
              alt={artifact.title ?? artifact.id}
              className="max-h-[440px] w-full rounded-md object-contain"
            />
          ) : (
            <p className="text-muted-foreground text-xs">图片地址缺失</p>
          )
        ) : kind === 'code' ? (
          // 复用 CodeBlock（含语法高亮 + 复制 + 语言头），不再用纯 <pre>
          <CodeBlock
            code={artifact.content ?? ''}
            language={artifact.language}
            // 制品预览嵌在内容区里，CodeBlock 自带外边距会撑出空白，关掉
            className="my-0"
          />
        ) : kind === 'data' ? (
          // JSON 走 CodeBlock 高亮（language='json'），统一代码展示体验
          <CodeBlock code={artifact.content ?? ''} language="json" className="my-0" />
        ) : kind === 'document' ? (
          // document 类型当 markdown 源渲染（复用 streamdown MarkdownRenderer）
          <MarkdownRenderer content={artifact.content ?? ''} />
        ) : (
          <p className="text-foreground whitespace-pre-wrap break-words text-sm">
            {artifact.content ?? ''}
          </p>
        )}
      </div>
    </div>
  )
}
