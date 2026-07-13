/**
 * 代码块组件
 *
 * 支持语法高亮和复制功能
 * 流式输出时不做语法高亮，避免性能抖动
 */

import { Check, Copy, Loader2 } from 'lucide-react'
import { type FC, memo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { cn } from '@/lib/utils'

export interface CodeBlockProps {
  /** 代码内容 */
  code: string
  /** 语言类型 */
  language?: string
  /** 是否显示行号 */
  showLineNumbers?: boolean
  /** 自定义类名 */
  className?: string
  /** 是否正在流式输出 */
  isStreaming?: boolean
}

/**
 * 代码块头部（显示语言和复制按钮）
 */
const CodeHeader: FC<{ language?: string; code: string; isStreaming?: boolean }> = ({
  language,
  code,
  isStreaming,
}) => {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    if (copied) return
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      console.error('复制失败:', err)
    }
  }

  return (
    <div className="bg-muted border-border flex items-center justify-between rounded-t-lg border-b px-4 py-2">
      <span className="text-muted-foreground font-mono text-xs lowercase">
        {language || 'text'}
      </span>
      <div className="flex items-center gap-2">
        {isStreaming && (
          <span className="text-muted-foreground flex items-center gap-1 text-xs">
            <Loader2 className="h-3 w-3 animate-spin" />
            输出中...
          </span>
        )}
        <button
          onClick={handleCopy}
          className={cn(
            'flex items-center gap-1.5 text-xs transition-colors',
            'text-muted-foreground hover:text-foreground',
          )}
          title="复制代码"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5 text-status-success" />
              <span className="text-status-success">已复制</span>
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" />
              <span>复制</span>
            </>
          )}
        </button>
      </div>
    </div>
  )
}

/**
 * 代码块组件
 *
 * 双阶段渲染策略：
 * - 流式阶段：不做语法高亮，直接显示代码文本
 * - 完成阶段：使用 SyntaxHighlighter 做语法高亮
 */
export const CodeBlock: FC<CodeBlockProps> = memo(
  ({ code, language, showLineNumbers = true, className, isStreaming = false }) => {
    const normalizedLanguage = language?.toLowerCase() || 'text'

    return (
      <div
        className={cn(
          'border-border my-4 overflow-hidden rounded-lg border',
          'max-w-full',
          className,
        )}
      >
        <CodeHeader language={normalizedLanguage} code={code} isStreaming={isStreaming} />
        {isStreaming ? (
          <pre
            className="overflow-x-auto p-4 text-sm"
            style={{
              background: 'var(--code-bg)',
              color: 'var(--code-text)',
              margin: 0,
              fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
            }}
          >
            <code>{code}</code>
          </pre>
        ) : (
          <SyntaxHighlighter
            language={normalizedLanguage}
            style={oneDark}
            showLineNumbers={showLineNumbers}
            wrapLongLines={true}
            customStyle={{
              margin: 0,
              borderRadius: '0 0 0.5rem 0.5rem',
              fontSize: '0.875rem',
              background: 'var(--code-bg)',
              maxWidth: '100%',
              overflowX: 'auto',
            }}
            codeTagProps={{
              style: {
                fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
              },
            }}
          >
            {code}
          </SyntaxHighlighter>
        )}
      </div>
    )
  },
)

CodeBlock.displayName = 'CodeBlock'
