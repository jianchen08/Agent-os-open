/**
 * Mermaid 图表组件
 *
 * 支持流程图、时序图、类图等多种图表类型
 *
 * 注意：需要安装依赖 mermaid
 */

import mermaid from 'mermaid'
import { memo, useEffect, useId, useRef, useState, type FC } from 'react'
import { cn } from '@/lib/utils'

/** 标记是否已初始化 */
let isInitialized = false

/**
 * 初始化 Mermaid 配置
 */
function initMermaid() {
  if (isInitialized) return

  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    securityLevel: 'loose',
    fontFamily: 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif',
    flowchart: {
      useMaxWidth: true,
      htmlLabels: true,
      curve: 'basis',
      padding: 0,
      diagramPadding: 0,
      nodeSpacing: 10,
      rankSpacing: 50,
    },
    sequence: {
      useMaxWidth: true,
      wrap: true,
      actorMargin: 10,
      boxMargin: 0,
      boxTextMargin: 0,
      noteMargin: 5,
      messageMargin: 20,
      diagramMarginX: 0,
      diagramMarginY: 0,
    },
    gantt: {
      useMaxWidth: true,
      titleTopMargin: 0,
      barHeight: 20,
      barGap: 4,
      topPadding: 10,
      gridLineStartPadding: 0,
    },
  })

  isInitialized = true
}

export interface MermaidDiagramProps {
  /** Mermaid 图表代码 */
  code: string
  /** 自定义类名 */
  className?: string
}

/**
 * 清理 Mermaid 代码
 */
function cleanMermaidCode(code: string): string {
  return code
    .trim()
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
}

/**
 * 检查 Mermaid 代码是否完整
 */
function isCompleteMermaidCode(code: string): boolean {
  const trimmed = code.trim()

  if (!trimmed) return false

  const hasGraphType =
    /^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|requirementDiagram|gitgraph|C4Context|mindmap|timeline|sankey|xychart|block-beta|packet-beta|architecture-beta|kanban)/i.test(
      trimmed,
    )
  if (!hasGraphType) return false

  const lines = trimmed.split('\n').filter((line) => line.trim())
  if (lines.length === 0) return false

  let braceCount = 0
  let bracketCount = 0
  let parenCount = 0
  let inString = false
  let stringChar = ''

  for (let i = 0; i < trimmed.length; i++) {
    const char = trimmed[i]
    const prevChar = i > 0 ? trimmed[i - 1] : ''

    if ((char === '"' || char === "'") && prevChar !== '\\') {
      if (!inString) {
        inString = true
        stringChar = char
      } else if (stringChar === char) {
        inString = false
        stringChar = ''
      }
      continue
    }

    if (inString) continue

    if (char === '{') braceCount++
    if (char === '}') braceCount--
    if (char === '[') bracketCount++
    if (char === ']') bracketCount--
    if (char === '(') parenCount++
    if (char === ')') parenCount--
  }

  if (braceCount !== 0 || bracketCount !== 0 || parenCount !== 0) return false

  const lastLine = lines[lines.length - 1].trim()
  if (/(-->|-->\||-.->|-.->\||==>|==>\||-\.->|-\.->\|)$/.test(lastLine)) return false

  const subGraphCount = (trimmed.match(/\bsubgraph\b/gi) || []).length
  const endCount = (trimmed.match(/\bend\b/gi) || []).length
  if (subGraphCount > endCount) return false

  return true
}

/**
 * Mermaid 图表渲染组件
 */
export const MermaidDiagram: FC<MermaidDiagramProps> = memo(({ code, className }) => {
  const containerRef = useRef<HTMLDivElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [isRendering, setIsRendering] = useState(true)
  const uniqueId = useId().replace(/:/g, '-')

  useEffect(() => {
    initMermaid()

    const renderDiagram = async () => {
      if (!containerRef.current) return

      const cleanedCode = cleanMermaidCode(code)
      if (!cleanedCode) {
        setError('图表代码为空')
        setIsRendering(false)
        return
      }

      if (!isCompleteMermaidCode(cleanedCode)) {
        setIsRendering(true)
        return
      }

      setIsRendering(true)
      setError(null)

      try {
        containerRef.current.innerHTML = ''

        const id = `mermaid-diagram-${uniqueId}-${Date.now()}`

        const { svg, bindFunctions } = await mermaid.render(id, cleanedCode)

        if (containerRef.current) {
          const parser = new DOMParser()
          const doc = parser.parseFromString(svg, 'image/svg+xml')
          const svgElement = doc.querySelector('svg')

          if (svgElement) {
            svgElement.removeAttribute('width')
            svgElement.removeAttribute('height')

            svgElement.style.width = '100%'
            svgElement.style.height = 'auto'
            svgElement.style.maxWidth = '100%'
            svgElement.style.display = 'block'
            svgElement.style.verticalAlign = 'bottom'
            svgElement.style.margin = '0'
            svgElement.style.padding = '0'
          }

          containerRef.current.innerHTML = doc.documentElement.outerHTML

          requestAnimationFrame(() => {
            const renderedSvg = containerRef.current?.querySelector('svg')
            if (renderedSvg && containerRef.current) {
              try {
                const bbox = renderedSvg.getBBox()
                renderedSvg.setAttribute(
                  'viewBox',
                  `${bbox.x} ${bbox.y} ${bbox.width} ${bbox.height}`,
                )
                renderedSvg.style.height = `${bbox.height}px`
                const totalHeight = bbox.height + 32
                containerRef.current.style.height = `${totalHeight}px`
                containerRef.current.style.minHeight = `${totalHeight}px`
                containerRef.current.style.maxHeight = `${totalHeight}px`
              } catch (e) {
                console.warn('获取 SVG bbox 失败:', e)
              }
            }
          })

          bindFunctions?.(containerRef.current)
        }
      } catch (err) {
        console.warn('Mermaid 渲染失败:', err)
        let errorMsg = '图表语法错误'
        if (err instanceof Error) {
          const match = err.message.match(/Parse error on line (\d+)/)
          if (match) {
            errorMsg = `第 ${match[1]} 行语法错误`
          } else if (err.message.includes('Syntax error')) {
            errorMsg = '图表语法错误，请检查格式'
          } else {
            errorMsg = err.message.slice(0, 100)
          }
        }
        setError(errorMsg)
      } finally {
        setIsRendering(false)
      }
    }

    renderDiagram()
  }, [code, uniqueId])

  if (error) {
    return (
      <div
        className={cn(
          'my-4 rounded-lg p-4',
          'bg-destructive/10 border-destructive/30 border',
          className,
        )}
      >
        <div className="text-destructive mb-2 text-sm font-medium">{error}</div>
        <details className="mt-2">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer text-xs">
            查看源代码
          </summary>
          <pre className="bg-muted mt-2 overflow-x-auto rounded-md p-3 font-mono text-xs">
            {code}
          </pre>
        </details>
      </div>
    )
  }

  return (
    <div className="my-4">
      <div
        className={cn(
          'relative overflow-hidden rounded-lg',
          'bg-muted/30 border-border border',
          className,
        )}
      >
        {isRendering && (
          <div className="text-muted-foreground py-8 text-center text-sm">正在渲染图表...</div>
        )}
        <div
          ref={containerRef}
          className={cn(isRendering && 'hidden')}
          data-testid="mermaid-container"
          style={{ padding: '1rem' }}
        />
      </div>
    </div>
  )
})

MermaidDiagram.displayName = 'MermaidDiagram'
