/**
 * ContentCardRenderer · 内容卡三形态渲染体（ADR 2026-10-14-content-card-registry §4）
 *
 * form=webview：沙箱 iframe 预览（srcdoc 无 allow-same-origin + CSP，不可信模型
 * 输出按安全形态处理）；form=css：fence 内容以文本节点注入容器内 pre（零解析零
 * 注入面），声明 css 主题化；form=template：宿主通用模板解释器装配。头部条提供
 * 源码视图↔渲染视图切换（源码视图 = 未命中路径原样 defaultView，保真现状）。
 * webview 卡经 useElementVisible 懒挂载（视口外不建 iframe，长会话性能 §8）。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useElementVisible } from '@/hooks/useElementVisible'
import {
  CONTENT_CARD_ROOT_CLASS,
  type ContentCardDeclaration,
} from '@/services/schema/contentCard'
import {
  bootstrapJs,
  CSP_META,
  WEBVIEW_MAX_HEIGHT,
  WEBVIEW_MIN_HEIGHT,
} from '@/components/schema/widgets/WebviewWidget'
import { validateWebviewEvent } from '@/utils/postMessageSecurity'
import { parseCsv } from './parseCsv'
import { renderTemplate } from './templateRenderer'

export interface ContentCardRendererProps {
  card: ContentCardDeclaration
  /** fence 内容原文（不可信模型输出——css/template 形态只经 textContent 进 DOM） */
  content: string
  /** 未命中路径原样元素（源码视图/折叠态复用，现状渲染保真锚） */
  defaultView: ReactNode
}

/** 卡内容高度夹取界（与 WebviewWidget autoHeight 同界） */
const CARD_MIN_HEIGHT = WEBVIEW_MIN_HEIGHT
const CARD_MAX_HEIGHT = WEBVIEW_MAX_HEIGHT

/**
 * 自报高度注入脚本：内容卡 webview 卡体的内容是**不可信模型输出**，卡内页
 * 不会自带 resize 上行（badge 卡那样的插件页才有）——宿主侧注入 ResizeObserver
 * 桥，内容尺寸变化（<details> 展开/收起、图片加载）即按实例令牌上行，
 * 容器贴合内容（2026-10-25 用户实测：固定 h-80 卡体在折叠态下留大片空白）。
 */
function autoresizeBootstrap(token: string): string {
  return (
    bootstrapJs(token, null) +
    `<script>
(function(){
  function post(){
    try {
      window.agentos.postMessage('webview.resize', { height: Math.ceil(document.body.scrollHeight) });
    } catch (e) {}
  }
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(post).observe(document.documentElement);
  }
  window.addEventListener('load', post);
  post();
})();
</script>`
  )
}

/** webview 形态卡体：沙箱 iframe（懒挂载 + 内容自报高度贴合，零空白） */
function WebviewCardBody({ content }: { content: string }) {
  const { ref, visible } = useElementVisible<HTMLDivElement>()
  const tokenRef = useRef<string>(
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `cc_${Math.random().toString(36).slice(2)}_${Date.now().toString(36)}`,
  )
  const [heightPx, setHeightPx] = useState(WEBVIEW_MIN_HEIGHT)
  useEffect(() => {
    const handler = (event: MessageEvent): void => {
      const msg = validateWebviewEvent(event, tokenRef.current)
      if (!msg || msg.method !== 'webview.resize') return
      const raw = (msg.params as { height?: unknown } | null)?.height
      if (typeof raw !== 'number' || !Number.isFinite(raw)) return
      setHeightPx(Math.min(CARD_MAX_HEIGHT, Math.max(CARD_MIN_HEIGHT, Math.round(raw))))
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [])
  if (!visible) {
    return <div ref={ref} className="border-border/40 bg-muted/30 h-80 w-full rounded-lg" />
  }
  return (
    <div ref={ref} className="w-full overflow-hidden" style={{ height: `${heightPx}px` }}>
      <iframe
        srcDoc={`<!DOCTYPE html><html><head>${CSP_META}</head><body>${content}${autoresizeBootstrap(tokenRef.current)}</body></html>`}
        title="content-card-preview"
        // 关键安全：不开 allow-same-origin → opaque origin，模型输出无法触宿主
        // DOM/cookie/localStorage（WebviewWidget/HtmlPreviewWidget 现役沙箱同款）
        sandbox="allow-scripts allow-forms allow-popups allow-modals"
        className="h-full w-full border-0 bg-transparent"
      />
    </div>
  )
}

/** template 形态卡体：宿主模板解释器装配（replaceChildren 挂载，React 不接管内部 DOM） */
function TemplateCardBody({ card, content }: { card: ContentCardDeclaration; content: string }) {
  const containerRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const needsCsv = String(card.template ?? '').includes('{{csv_table}}')
    const csvRows = needsCsv ? parseCsv(content) : null
    container.replaceChildren(renderTemplate(card.template ?? '', content, csvRows))
  }, [card, content])
  return <div ref={containerRef} className="agentos-content-card-body" />
}

/** 内容卡渲染器：头部条（标题/来源/切换）+ 卡体（三形态）；chromeless 声明
 * 去头部件与边框——卡体即全部（todo <details> 卡等自带折叠的形态） */
export function ContentCardRenderer({ card, content, defaultView }: ContentCardRendererProps) {
  // 缺省态随声明（default_collapsed，ADR §1）；声明变更（热重载）时回落新缺省
  const [collapsed, setCollapsed] = useState(card.default_collapsed ?? false)
  const declaredDefault = card.default_collapsed ?? false
  useEffect(() => {
    setCollapsed(declaredDefault)
  }, [declaredDefault])
  const chromeless = card.chromeless === true

  return (
    <div
      className={`${CONTENT_CARD_ROOT_CLASS} overflow-hidden rounded-lg ${
        chromeless ? '' : 'border-border/40 my-2 border'
      }`}
      data-content-card={card.id}
      data-content-card-plugin={card.pluginId}
    >
      {card.css ? <style>{card.css}</style> : null}
      {!chromeless && (
        <div className="bg-muted/40 text-muted-foreground flex items-center gap-2 px-3 py-1.5 text-xs">
          <span className="font-medium">{card.title ?? card.id}</span>
          <span className="opacity-60">{card.pluginId === 'host' ? '内置' : card.pluginId}</span>
          <button
            type="button"
            data-testid="content-card-toggle"
            className="hover:text-foreground ml-auto cursor-pointer"
            onClick={() => setCollapsed((v) => !v)}
          >
            {collapsed ? '渲染视图' : '源码视图'}
          </button>
        </div>
      )}
      {!chromeless && collapsed ? (
        defaultView
      ) : card.form === 'webview' ? (
        <WebviewCardBody content={content} />
      ) : card.form === 'css' ? (
        <pre className="max-h-96 overflow-auto p-3 text-sm" data-testid="content-card-css-body">
          {content}
        </pre>
      ) : (
        <TemplateCardBody card={card} content={content} />
      )}
    </div>
  )
}
