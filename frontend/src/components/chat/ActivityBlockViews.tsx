/**
 * ActivityCard 明细块视图家族
 *
 * 从 ActivityCard 拆出（冻结文件只许缩小的拆分管线）：各 detail block 的
 * 渲染视图 + 分发器 DetailBlock；主卡片仅消费 DetailBlock。
 */
import { useEffect, useRef, useState } from 'react'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  FileText,
  Link,
  Loader2,
} from '@/assets/icons'
import { TextDiffView } from '@/components/approval'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import { TOOL_CONTENT_SCROLL_CLASS } from '@/lib/toolCardStyles'
import { cn } from '@/lib/utils'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { ActivityDetailBlock } from '@/types/activity'
import type { FC } from 'react'

const CopyBtn: FC<{ text: string }> = ({ text }) => {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation()
        try {
          await navigator.clipboard.writeText(text)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        } catch {
          // 剪贴板不可用时静默
        }
      }}
      className="text-muted-foreground hover:text-foreground absolute top-1 right-1 rounded p-1 transition-colors"
      title="复制"
      aria-label="复制内容"
    >
      {copied ? (
        <Check className="h-icon-sm w-icon-sm text-status-success" />
      ) : (
        <Copy className="h-icon-sm w-icon-sm" />
      )}
    </button>
  )
}

/**
 * 文件块：文件名 + 路径，点击打开文件（统一走全局文件打开回调）
 */
const FileBlockView: FC<{ path: string }> = ({ path }) => {
  const fileName = path.split(/[/\\]/).pop() || path
  return (
    <button
      onClick={() => getGlobalOpenFileCallback()(path)}
      className="group flex max-w-full items-center gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-[var(--hover-overlay)]"
      title={`点击打开文件: ${path}`}
    >
      <FileText className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
      <span className="text-primary min-w-0 truncate font-medium group-hover:underline">{fileName}</span>
      <span className="text-muted-foreground/70 min-w-0 max-w-[320px] truncate font-mono text-[11px]">
        {path}
      </span>
      <ExternalLink className="text-muted-foreground/50 h-icon-xs w-icon-xs flex-shrink-0" />
    </button>
  )
}

/**
 * 图片块：缩略图 + 点击灯箱预览；加载失败降级为文件行
 */
const ImageBlockView: FC<{ src: string }> = ({ src }) => {
  const [open, setOpen] = useState(false)
  const [failed, setFailed] = useState(false)

  if (failed) {
    return <FileBlockView path={src} />
  }

  return (
    <>
      <img
        src={src}
        alt="预览图"
        loading="lazy"
        onError={() => setFailed(true)}
        onClick={() => setOpen(true)}
        className="ring-border/40 max-h-40 cursor-zoom-in rounded object-contain transition-shadow hover:ring-1"
      />
      {open && (
        <div
          className="bg-[var(--overlay-strong)] fixed inset-0 z-50 flex cursor-zoom-out items-center justify-center p-6"
          onClick={() => setOpen(false)}
          role="dialog"
          aria-modal="true"
          aria-label="图片预览"
        >
          <img
            src={src}
            alt="大图预览"
            className="max-h-[85vh] max-w-[90vw] rounded object-contain shadow-2xl"
          />
        </div>
      )}
    </>
  )
}

/**
 * 链接块：点击外部浏览器打开
 *
 * href 走 http(s) 协议白名单（对齐 WebBlock.safeHref）：LLM 活动块解析出的
 * url 不可信，file:/自定义协议一律降级为不可点击文本。
 */
const SAFE_HREF_RE = /^https?:\/\//i

const LinkBlockView: FC<{ url: string }> = ({ url }) => (
  <a
    href={SAFE_HREF_RE.test(url) ? url : undefined}
    target="_blank"
    rel="noreferrer"
    className="text-primary inline-flex max-w-full items-center gap-1.5 truncate hover:underline"
    title={url}
  >
    <Link className="h-icon-sm w-icon-sm flex-shrink-0" />
    <span className="truncate">{url}</span>
  </a>
)

/**
 * 键值对块：key 弱化 / value 等宽两列
 */
const KvBlockView: FC<{ items: { key: string; value: string }[] }> = ({ items }) => (
  <div className="bg-muted/30 space-y-1 rounded p-2">
    {items.map((item, index) => (
      <div key={`${item.key}-${index}`} className="flex min-w-0 items-baseline gap-2 text-xs">
        <span className="text-muted-foreground w-28 flex-shrink-0 truncate">{item.key}</span>
        <span className="min-w-0 truncate font-mono">{item.value}</span>
      </div>
    ))}
  </div>
)

/**
 * 表格块：表头 + 二维数组行（横向滚动；行列数大时纵向限高滚动）
 */
const TableBlockView: FC<{ columns: string[]; rows: string[][] }> = ({ columns, rows }) => (
  <div className="bg-muted/30 overflow-x-auto rounded">
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr>
          {columns.map((col, i) => (
            <th
              key={`h-${i}`}
              className="text-muted-foreground border-border/40 bg-muted/40 border-b px-2 py-1 text-left font-medium whitespace-nowrap"
            >
              {col}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={`r-${ri}`} className="odd:bg-transparent even:bg-[var(--hover-overlay)]">
            {columns.map((_c, ci) => (
              <td key={`c-${ci}`} className="text-foreground/90 max-w-[320px] truncate border-b border-[var(--border-border)]/30 px-2 py-1 font-mono whitespace-nowrap">
                {row[ci] ?? ''}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
)

/**
 * 表单块：标量 kv 平铺 + 长文本/对象折叠区（渲染路由 form 卡产物）。
 * 长内容按 label 折叠（默认收起，避免大文本撑爆卡片）。
 */
const FormBlockView: FC<{
  items: { key: string; value: string }[]
  jsonItems: { label: string; content: unknown }[]
}> = ({ items, jsonItems }) => {
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set())
  return (
    <div className="space-y-2">
      {items.length > 0 && <KvBlockView items={items} />}
      {jsonItems.map((item, index) => {
        const text =
          typeof item.content === 'string'
            ? item.content
            : JSON.stringify(item.content, null, 2)
        const isExpanded = expandedKeys.has(item.label)
        return (
          <div key={`${item.label}-${index}`} className="bg-muted/30 rounded">
            <button
              onClick={() => {
                setExpandedKeys((prev) => {
                  const next = new Set(prev)
                  if (next.has(item.label)) next.delete(item.label)
                  else next.add(item.label)
                  return next
                })
              }}
              className="text-muted-foreground hover:text-foreground flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-xs font-medium transition-colors"
            >
              {isExpanded ? (
                <ChevronDown className="h-icon-xs w-icon-xs flex-shrink-0" />
              ) : (
                <ChevronRight className="h-icon-xs w-icon-xs flex-shrink-0" />
              )}
              <span className="min-w-0 truncate">{item.label}</span>
              <span className="text-muted-foreground/60 ml-auto shrink-0">{text.length} 字符</span>
            </button>
            {isExpanded && (
              <pre className={`${TOOL_CONTENT_SCROLL_CLASS} max-h-48 overflow-y-auto p-2 pt-0 font-mono text-xs whitespace-pre-wrap`}>
                {text}
              </pre>
            )}
          </div>
        )
      })}
    </div>
  )
}

/**
 * 交互表单块（widget 化 T2/T4）：chat_card form 声明 / output_schema 结构化视图。
 *
 * 双形态：endpoint 有值 = 交互表单（FormWidget endpoint 模式提交）；
 * readOnly/无 endpoint = 只读结构化展示（整表 disabled，字段模式无提交按钮）。
 * 点击/提交事件阻断冒泡（卡片头部折叠开关不误触发）。
 */
const FormBlockInputView: FC<{
  fields: unknown[]
  endpoint?: string
  values?: Record<string, unknown>
  submitLabel?: string
  readOnly?: boolean
}> = ({ fields, endpoint, values, submitLabel, readOnly }) => (
  <div
    className="py-1"
    onClick={(e) => e.stopPropagation()}
    onSubmit={(e) => e.stopPropagation()}
  >
    <FormWidget
      fields={fields}
      endpoint={readOnly ? undefined : endpoint}
      initialValues={values}
      submitLabel={submitLabel}
      layout="single"
      disabled={readOnly}
    />
  </div>
)

/**
 * 日志块：等宽滚动区，吸底滚动 + 上翻滚动锁
 */
const LogBlockView: FC<{ content: string }> = ({ content }) => {
  const ref = useRef<HTMLPreElement>(null)

  useEffect(() => {
    const el = ref.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 48) {
      el.scrollTop = el.scrollHeight
    }
  }, [content])

  return (
    <div className="group relative">
      <pre
        ref={ref}
        className="bg-muted/30 max-h-60 overflow-y-auto rounded p-2 pr-7 font-mono text-xs whitespace-pre-wrap"
      >
        {content}
      </pre>
      <CopyBtn text={content} />
    </div>
  )
}

/**
 * 文件内容块：行号 gutter + 窗口计数（render 意图 read 卡产物）。
 *
 * 窗口计数仅在返回行数少于文件总行数时展示——展示"当前窗口 / 全文"关系，
 * 不给截断假象（无窗口时总数无信息量）。
 */
const ReadBlockView: FC<{ read: NonNullable<ActivityDetailBlock['read']> }> = ({ read }) => {
  const { lines, totalLines, lang } = read
  const raw = lines.map((line) => line.text).join('\n')
  const windowed = lines.length < totalLines
  return (
    <div className="bg-muted/30 overflow-hidden rounded" data-testid="read-block">
      <div className="text-muted-foreground flex items-center gap-2 border-b border-border px-2 py-1 text-xs">
        {windowed && <span data-testid="read-window-count">{`显示 ${lines.length} / ${totalLines} 行`}</span>}
        {lang && <span className="ml-auto">{lang}</span>}
        <CopyBtn text={raw} />
      </div>
      <div className={`${TOOL_CONTENT_SCROLL_CLASS} font-mono text-xs`}>
        {lines.map((line) => (
          <div key={line.number} className="flex">
            <span className="text-muted-foreground/50 w-10 shrink-0 select-none border-r border-border px-1 text-right" aria-hidden>
              {line.number}
            </span>
            <span className="flex-1 px-1.5 whitespace-pre-wrap break-words">{line.text}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * 命令块：命令头（cwd + 退出码）+ 输出区（render 意图 terminal 卡产物）。
 *
 * 输出为纯文本——ANSI 转义序列由文本层兜底渲染（不解析配色），
 * 退出码非零时以状态色标注（失败可见）。
 */
const TerminalBlockView: FC<{ terminal: NonNullable<ActivityDetailBlock['terminal']> }> = ({ terminal }) => {
  const { command, cwd, output, exitCode, running } = terminal
  return (
    <div className="bg-muted/30 overflow-hidden rounded" data-testid="terminal-block">
      <div className="flex items-center gap-2 border-b border-border px-2 py-1 font-mono text-xs">
        <span className="min-w-0 flex-1 truncate" title={command}>
          <span className="text-muted-foreground/60 select-none">$ </span>
          {command}
        </span>
        {cwd && <span className="text-muted-foreground/70 shrink-0 truncate max-w-[160px]">{cwd}</span>}
        {running ? (
          <Loader2 className="text-status-running h-icon-sm w-icon-sm shrink-0 animate-spin" />
        ) : exitCode !== undefined ? (
          <span className={exitCode === 0 ? 'text-status-success shrink-0' : 'text-status-error shrink-0'}>
            {`exit ${exitCode}`}
          </span>
        ) : null}
      </div>
      {output !== '' && (
        <div className="group relative">
          <pre className={`${TOOL_CONTENT_SCROLL_CLASS} p-2 pr-7 font-mono text-xs`}>{output}</pre>
          <CopyBtn text={output} />
        </div>
      )}
    </div>
  )
}

/**
 * 搜索结果块：matches（文件分组，可折叠）| paths（平铺列表）。
 *
 * 分组折叠默认展开首个文件组——命中集中在少数文件时免去逐组点击；
 * truncated 时展示「显示 X / 共 N」避免把截断结果当完整（对齐后端 total 字段）。
 */
const SearchBlockView: FC<{ search: NonNullable<ActivityDetailBlock['search']> }> = ({ search }) => {
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())

  if (search.kind === 'paths') {
    const { paths, truncated, total } = search
    return (
      <div className="bg-muted/30 overflow-hidden rounded" data-testid="search-block">
        <div className="text-muted-foreground flex items-center border-b border-border px-2 py-1 text-xs">
          <span data-testid="search-count">{truncated ? `显示 ${paths.length} / 共 ${total} 个路径` : `${paths.length} 个路径`}</span>
          <CopyBtn text={paths.join('\n')} />
        </div>
        <div className={`${TOOL_CONTENT_SCROLL_CLASS} font-mono text-xs`}>
          {paths.map((p) => (
            <div key={p} className="px-2 py-0.5 break-all">{p}</div>
          ))}
        </div>
      </div>
    )
  }

  const { files, truncated, total } = search
  const shown = files.reduce((sum, f) => sum + f.matches.length, 0)
  const copyText = files.map((f) => [f.path, ...f.matches.map((m) => `${m.lineNumber}: ${m.line}`)].join('\n')).join('\n\n')
  return (
    <div className="bg-muted/30 overflow-hidden rounded" data-testid="search-block">
      <div className="text-muted-foreground flex items-center border-b border-border px-2 py-1 text-xs">
        <span data-testid="search-count">{truncated ? `显示 ${shown} / 共 ${total} 条` : `${shown} 条匹配`}</span>
        <CopyBtn text={copyText} />
      </div>
      <div className={`${TOOL_CONTENT_SCROLL_CLASS} text-xs`}>
        {files.map((f, fi) => {
          const isCollapsed = collapsed.has(fi)
          return (
            <div key={`${f.path}-${fi}`}>
              <button
                type="button"
                onClick={() =>
                  setCollapsed((prev) => {
                    const next = new Set(prev)
                    if (next.has(fi)) next.delete(fi)
                    else next.add(fi)
                    return next
                  })
                }
                className="hover:bg-[var(--hover-overlay)] flex w-full items-center gap-1 px-1.5 py-0.5 text-left font-medium"
              >
                {isCollapsed ? <ChevronRight className="h-icon-sm w-icon-sm" /> : <ChevronDown className="h-icon-sm w-icon-sm" />}
                <span className="text-primary min-w-0 truncate font-mono">{f.path}</span>
                <span className="text-muted-foreground/70 shrink-0">{`${f.matches.length} 处`}</span>
              </button>
              {!isCollapsed &&
                f.matches.map((m) => (
                  <div key={m.lineNumber} className="flex px-1.5 py-0.5 font-mono">
                    <span className="text-muted-foreground/50 w-10 shrink-0 select-none text-right" aria-hidden>
                      {m.lineNumber}
                    </span>
                    <span className="flex-1 px-1.5 whitespace-pre-wrap break-words">{m.line}</span>
                  </div>
                ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/**
 * 联网结果块：fetch（URL + 状态码）| search（回答 + 来源列表）。
 *
 * 来源链接仅 http(s) 可点击（LLM 产出的 url 不可信，其它协议降级为纯文本，
 * 与 LinkBlockView 同一白名单口径）。
 */
const WebBlockView: FC<{ web: NonNullable<ActivityDetailBlock['web']> }> = ({ web }) => {
  if (web.kind === 'fetch') {
    const { url, statusCode, truncated } = web
    const ok = statusCode >= 200 && statusCode < 400
    return (
      <div className="bg-muted/30 flex items-center gap-2 rounded p-2 text-xs" data-testid="web-block">
        <span className={ok ? 'text-status-success shrink-0' : 'text-status-error shrink-0'}>{statusCode}</span>
        {SAFE_HREF_RE.test(url) ? (
          <a href={url} target="_blank" rel="noreferrer" className="text-primary min-w-0 flex-1 truncate hover:underline" title={url}>
            {url}
          </a>
        ) : (
          <span className="min-w-0 flex-1 truncate" title={url}>{url}</span>
        )}
        {truncated && <span className="text-muted-foreground/70 shrink-0">已截断</span>}
      </div>
    )
  }

  const { answer, sources, truncated } = web
  return (
    <div className="bg-muted/30 space-y-2 rounded p-2 text-xs" data-testid="web-block">
      {answer && <div className="max-w-none">{answer}</div>}
      {truncated && <div className="text-muted-foreground/70">来源列表已截断</div>}
      <div className="space-y-1">
        {sources.map((s, i) => (
          <div key={`${s.url}-${i}`} className="flex min-w-0 flex-col">
            {SAFE_HREF_RE.test(s.url) ? (
              <a href={s.url} target="_blank" rel="noreferrer" className="text-primary truncate hover:underline" title={s.url}>
                {s.title ?? s.url}
              </a>
            ) : (
              <span className="truncate" title={s.url}>{s.title ?? s.url}</span>
            )}
            {s.snippet && <span className="text-muted-foreground line-clamp-2">{s.snippet}</span>}
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * 详情区块组件
 */
const DiffDetailView: FC<{ oldContent: string; newContent: string }> = ({
  oldContent,
  newContent,
}) => {
  const [mode, setMode] = useState<'diff' | 'full'>('diff')
  const fullLines = newContent.split('\n')

  return (
    <div className="bg-muted/30 overflow-x-auto rounded" data-testid="diff-detail-view">
      {newContent !== '' && (
        <div
          className="flex items-center gap-1 border-b border-border px-2 py-1"
          role="tablist"
          aria-label="差异视图切换"
        >
          {(['diff', 'full'] as const).map((m) => (
            <button
              key={m}
              role="tab"
              aria-selected={mode === m}
              data-testid={`diff-view-${m}`}
              onClick={() => setMode(m)}
              className={cn(
                'rounded px-2 py-0.5 text-xs transition-colors',
                mode === m
                  ? 'bg-[var(--hover-overlay)] font-medium text-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {m === 'diff' ? '差异对比' : '完整文件'}
            </button>
          ))}
        </div>
      )}
      {mode === 'diff' ? (
        <TextDiffView oldContent={oldContent} newContent={newContent} />
      ) : (
        <div className="font-mono text-xs" data-testid="diff-full-content">
          {fullLines.map((line, i) => (
            <div key={i} className="flex" data-testid={`full-line-${i}`}>
              <span className="text-muted-foreground/50 w-8 shrink-0 select-none border-r border-border px-1 text-right">
                {i + 1}
              </span>
              <span className="flex-1 px-1 break-all whitespace-pre-wrap">{line}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * 详情区块组件
 */
export const DetailBlock: FC<{ block: ActivityDetailBlock }> = ({ block }) => {
  const [expanded, setExpanded] = useState(block.defaultExpanded ?? true)

  /** 渲染内容 */
  const renderContent = () => {
    const content = block.content
    const contentType = block.contentType || 'text'

    // render 意图产出的原生卡片块（renderIntent.ts 声明/数据路由）：
    // 结构化字段直取，先于下方 object→JSON 兜底判定。
    if (contentType === 'read' && block.read) {
      return <ReadBlockView read={block.read} />
    }
    if (contentType === 'terminal' && block.terminal) {
      return <TerminalBlockView terminal={block.terminal} />
    }
    if (contentType === 'search' && block.search) {
      return <SearchBlockView search={block.search} />
    }
    if (contentType === 'web' && block.web) {
      return <WebBlockView web={block.web} />
    }

    // 交互表单块（widget 化 T2/T4）：content.formFields 数组 = 声明表单 /
    // 契约结构化视图（需先于 object→JSON 兜底——content 是结构化 props）；
    // 旧形态（kvItems/jsonItems 只读）落下方 switch 的 FormBlockView。
    if (
      contentType === 'form' &&
      content &&
      typeof content === 'object' &&
      Array.isArray((content as Record<string, unknown>).formFields)
    ) {
      const c = content as {
        formFields: unknown[]
        endpoint?: string
        values?: Record<string, unknown>
        submitLabel?: string
        readOnly?: boolean
      }
      return (
        <FormBlockInputView
          fields={c.formFields}
          endpoint={c.endpoint}
          values={c.values}
          submitLabel={c.submitLabel}
          readOnly={c.readOnly}
        />
      )
    }

    if (typeof content === 'object') {
      const text = JSON.stringify(content, null, 2)
      return (
        <div className="group relative">
          <pre className={`bg-muted/30 ${TOOL_CONTENT_SCROLL_CLASS} rounded p-2 pr-7 font-mono text-xs`}>
            {text}
          </pre>
          <CopyBtn text={text} />
        </div>
      )
    }

    switch (contentType) {
      case 'json':
        try {
          const parsed = JSON.parse(content)
          const text = JSON.stringify(parsed, null, 2)
          return (
            <div className="group relative">
              <pre className={`bg-muted/30 ${TOOL_CONTENT_SCROLL_CLASS} rounded p-2 pr-7 font-mono text-xs`}>
                {text}
              </pre>
              <CopyBtn text={text} />
            </div>
          )
        } catch {
          return (
            <div className="group relative">
              <pre className={`bg-muted/30 ${TOOL_CONTENT_SCROLL_CLASS} rounded p-2 pr-7 font-mono text-xs`}>
                {content}
              </pre>
              <CopyBtn text={content} />
            </div>
          )
        }

      case 'code':
        return (
          <div className="group relative">
            <pre
              className={cn(
                'bg-muted/30 overflow-x-auto rounded p-2 pr-7 font-mono text-xs',
                block.language && `language-${block.language}`,
              )}
            >
              <code>{content}</code>
            </pre>
            <CopyBtn text={content} />
          </div>
        )

      case 'diff':
        return (
          <DiffDetailView
            oldContent={block.diffOld ?? ''}
            newContent={block.diffNew ?? ''}
          />
        )

      case 'markdown':
        return (
          <div className={`bg-muted/30 max-w-none rounded p-2 text-xs ${TOOL_CONTENT_SCROLL_CLASS}`}>
            <MarkdownRenderer content={content} />
          </div>
        )

      case 'kv':
        return <KvBlockView items={block.kvItems ?? []} />

      case 'table':
        return (
          <TableBlockView
            columns={block.table?.columns ?? []}
            rows={block.table?.rows ?? []}
          />
        )

      case 'form':
        return <FormBlockView items={block.kvItems ?? []} jsonItems={block.jsonItems ?? []} />

      case 'file':
        return <FileBlockView path={block.path || content} />

      case 'image':
        return <ImageBlockView src={block.path || content} />

      case 'link':
        return <LinkBlockView url={block.url || content} />

      case 'log':
        return <LogBlockView content={content} />

      case 'text':
      default:
        return (
          <pre className={`bg-muted/30 ${TOOL_CONTENT_SCROLL_CLASS} rounded p-2 text-xs`}>
            {content}
          </pre>
        )
    }
  }

  if (!block.collapsible) {
    return (
      <div className="space-y-1.5">
        <div className="text-muted-foreground text-xs font-medium">{block.label}</div>
        {renderContent()}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-xs font-medium transition-colors"
      >
        {expanded ? (
          <ChevronDown className="h-icon-sm w-icon-sm" />
        ) : (
          <ChevronRight className="h-icon-sm w-icon-sm" />
        )}
        {block.label}
      </button>
      {expanded && renderContent()}
    </div>
  )
}

