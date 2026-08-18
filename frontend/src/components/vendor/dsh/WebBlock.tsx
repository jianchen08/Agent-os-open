/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/WebBlock.tsx
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5).
 * Adapted for AgentOS (task_dsh_plugin_adapter 任务 3):
 * - MarkdownText（DSH mdast 增量渲染全家桶）→ 灵汐 MarkdownRenderer
 *   （react-markdown + gfm），等价渲染 answer 富文本；
 * - SafeLink http(s) 白名单/来源列表滚动容器/截断标注逻辑保持原样。
 */

import clsx from 'clsx'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import css from './WebBlock.module.css'

/**
 * One citeable source drawn in a search card: the projection of the contract's
 * `WebSource`, with the optional fields kept optional so a provider that
 * returned only a URL still renders (its hostname becomes the label).
 */
export interface WebSourceView {
  /** The source URL; becomes a safe external link when it is http(s). */
  url: string
  /** The source title; when absent the URL's hostname labels the link. */
  title?: string | undefined
  /** A short excerpt or summary shown under the link. */
  snippet?: string | undefined
  /** Publication/crawl timestamp, a provider-supplied string shown under the link. */
  publishedAt?: string | undefined
}

/** A `web_search` card: an optional answer over a capped citation list. */
export interface WebSearchBlockProps {
  kind: 'search'
  /** The provider-generated answer, rendered as markdown above the sources. */
  answer?: string | undefined
  /** The cited sources, in provider order. */
  sources: WebSourceView[]
  /** True when the tool cut the source list to its result cap. */
  truncated: boolean
  /** Extra class merged onto the wrapper (callers position; this component draws). */
  className?: string | undefined
}

/** A `web_fetch` card: the retrieval summary for one fetched URL. */
export interface WebFetchBlockProps {
  kind: 'fetch'
  /** The final URL after allowed redirects; becomes a safe external link when http(s). */
  url: string
  /** HTTP status code of the fetched response. */
  statusCode: number
  /** True when the provider or the output cap cut the fetched content. */
  truncated: boolean
  /** Extra class merged onto the wrapper (callers position; this component draws). */
  className?: string | undefined
}

/** A completed web retrieval card, discriminated by `kind`. */
export type WebBlockProps = WebSearchBlockProps | WebFetchBlockProps

/**
 * The URL to link to, or undefined when the URL must render as plain text. Only
 * http(s) becomes a navigable external anchor, so a `javascript:`/`data:`/`file:`
 * URL or an unparseable string never reaches the DOM as an href.
 * @param url - the source or fetch URL, from tool result content.
 * @returns the href to use, or undefined for plain text.
 */
function safeHref(url: string): string | undefined {
  try {
    const { protocol } = new URL(url)
    return protocol === 'http:' || protocol === 'https:' ? url : undefined
  } catch {
    return undefined
  }
}

/**
 * The link's visible label: the title when the provider gave one, otherwise the
 * URL's hostname, falling back to the raw URL when it does not parse, so a
 * label is never blank.
 * @param url - the source URL.
 * @param title - the provider title, if any.
 * @returns the label text.
 */
function linkLabel(url: string, title: string | undefined): string {
  if (title !== undefined && title !== '') return title
  try {
    const { hostname } = new URL(url)
    return hostname === '' ? url : hostname
  } catch {
    return url
  }
}

/**
 * A single URL rendered as a safe external anchor, or as plain text when the
 * URL is not an http(s) link.
 * @param props.url - the URL to render.
 * @param props.label - the visible label.
 * @param props.className - class for the anchor or the plain span.
 * @returns the anchor or span element.
 */
function SafeLink({ url, label, className }: { url: string; label: string; className?: string | undefined }) {
  const href = safeHref(url)
  if (href === undefined) return <span className={className}>{label}</span>
  return (
    <a className={className} href={href} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  )
}

/**
 * One source row in a search card: the safe link plus its snippet and date.
 * @param props.source - the source to render.
 * @param props.ordinal - the source's 1-based position in the full list.
 * @returns the source list item.
 */
function SourceItem({ source, ordinal }: { source: WebSourceView; ordinal: number }) {
  return (
    <li className={css.source} value={ordinal}>
      <SafeLink url={source.url} label={linkLabel(source.url, source.title)} className={css.sourceLink} />
      {source.snippet !== undefined && source.snippet !== '' && (
        <div className={css.snippet}>{source.snippet}</div>
      )}
      {source.publishedAt !== undefined && source.publishedAt !== '' && (
        <div className={css.published}>{source.publishedAt}</div>
      )}
    </li>
  )
}

/**
 * The search card body: the answer over the full source list, which scrolls in
 * place once it exceeds the `.sources` container height.
 * @param props - see {@link WebSearchBlockProps}.
 * @returns the search card element.
 */
function WebSearchBlock({ answer, sources, truncated, className }: WebSearchBlockProps) {
  // A provider may legitimately return no answer and no sources; without this
  // the user would see an empty card. Mirror the backend's render text.
  const empty = (answer === undefined || answer === '') && sources.length === 0
  return (
    <div className={clsx(css.block, className)} data-web="search">
      {answer !== undefined && answer !== '' && (
        <div className={css.answer}><MarkdownRenderer content={answer} /></div>
      )}
      {empty ? (
        <div className={css.empty}>未找到结果</div>
      ) : (
        <ol className={css.sources}>
          {sources.map((source, index) => <SourceItem key={index} source={source} ordinal={index + 1} />)}
        </ol>
      )}
      {truncated && <div className={css.truncated}>来源列表已截断</div>}
    </div>
  )
}

/**
 * The fetch card body: the linked URL and its HTTP status.
 * @param props - see {@link WebFetchBlockProps}.
 * @returns the fetch card element.
 */
function WebFetchBlock({ url, statusCode, truncated, className }: WebFetchBlockProps) {
  return (
    <div className={clsx(css.block, css.fetch, className)} data-web="fetch">
      <SafeLink url={url} label={url} className={css.fetchUrl} />
      <div className={css.fetchMeta}>
        <span className={css.status}>HTTP {statusCode}</span>
        {truncated && <span className={css.truncated}>内容已截断</span>}
      </div>
    </div>
  )
}

/**
 * Render a completed web retrieval as a structured card.
 * @param props - see {@link WebBlockProps}; `kind` selects the search or fetch body.
 * @returns the web card element.
 */
export function WebBlock(props: WebBlockProps) {
  return props.kind === 'search' ? <WebSearchBlock {...props} /> : <WebFetchBlock {...props} />
}
