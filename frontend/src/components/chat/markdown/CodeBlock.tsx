/**
 * 代码块组件
 *
 * 支持语法高亮和复制功能
 * 流式输出时不做语法高亮，避免性能抖动
 */

import { cn } from '@/lib/utils'
import { Check, Copy, Loader2 } from 'lucide-react'
import { type FC, memo, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

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
    <div className="flex items-center justify-between px-4 py-2 bg-muted border-b border-border rounded-t-lg">
      <span className="text-xs text-muted-foreground font-mono lowercase">
        {language || 'text'}
      </span>
      <div className="flex items-center gap-2">
        {isStreaming && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="w-3 h-3 animate-spin" />
            输出中...
          </span>
        )}
        <button
          onClick={handleCopy}
          className={cn(
            'flex items-center gap-1.5 text-xs transition-colors',
            'text-muted-foreground hover:text-foreground'
          )}
          title="复制代码"
        >
          {copied ? (
            <>
              <Check className="w-3.5 h-3.5 text-green-500" />
              <span className="text-green-500">已复制</span>
            </>
          ) : (
            <>
              <Copy className="w-3.5 h-3.5" />
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
          'my-4 rounded-lg overflow-hidden border border-border',
          'max-w-full',
          className
        )}
      >
        <CodeHeader language={normalizedLanguage} code={code} isStreaming={isStreaming} />
        {isStreaming ? (
          <pre
            className="p-4 overflow-x-auto text-sm"
            style={{
              background: '#1e1e1e',
              color: '#d4d4d4',
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
              background: '#1e1e1e',
              maxWidth: '100%',
              overflowX: 'auto',
            }}
            codeTagProps={{
              style: {
                fontFamily:
                  'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
              },
            }}
          >
            {code}
          </SyntaxHighlighter>
        )}
      </div>
    )
  }
)

CodeBlock.displayName = 'CodeBlock'
