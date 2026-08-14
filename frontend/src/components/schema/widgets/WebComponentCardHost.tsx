/**
 * WebComponentCardHost — 第二条插件 UI 注入路径（ADR §3.4 第二通道）
 *
 * @deprecated 自 0.2 起废弃（任务域：插件前端定制化，见 docs/tasks/task_plugin_frontend_customization.md）。
 * 三个废弃理由：
 * 1. **eval 安全风险**：new Function（等价 eval）在主页全局执行插件 JS，
 *    能偷 token、改 DOM、监听事件——源码安全假设已不成立（等价 bash 访问）；
 * 2. **Shadow DOM 隔离改不到现有 UI**：CE 的 CSS 只影响自身 Shadow DOM 内部，
 *    做不了"金色蕾丝边框"这类宿主 UI 视觉定制；
 * 3. **无不可替代能力**：高频交互被预置 Widget 覆盖、动态组件被 WebviewWidget
 *    覆盖、改视觉被「主题插件（contributes.themes）+ CSS 注入（contributes.client_styles）」覆盖。
 *
 * 迁移路径（按需选择）：
 * - 要改宿主视觉（边框/动画/背景）→ 主题插件（变量）或 CSS 注入（装饰规则）；
 * - 要提供完整自定义交互组件 → WebviewWidget（iframe 沙箱，见同目录 WebviewWidget.tsx）。
 * 本组件暂不删除（兼容现有依赖），确认无插件引用后移除。
 *
 * 与 WebviewWidget（iframe srcDoc sandbox）互补：
 * - WebviewWidget：iframe 沙箱，postMessage 通信，**绝不开 allow-same-origin**，最安全但重
 * - WebComponentCardHost：插件提供 JS（注册成 Custom Element），前端动态加载执行，
 *   直接把 props 传给元素。同进程、无 postMessage 序列化，适合轻量/高频交互组件
 *
 * 数据流：
 *   插件 http.handle → `/ext/{pluginId}{scriptPath}` 返回 JS 文本（后端 dispatcher 透传 Content-Type）
 *   → apiClient.get（带 Bearer）fetch JS → new Function(script) 执行
 *   → 脚本内 customElements.define(tagName, ...) 生效
 *   → 宿主 createElement(tagName) + appendChild + 设 props
 *
 * 隔离模型（Shadow DOM）：
 *   Custom Element 的 Shadow DOM 由元素**类自身**负责（在 connectedCallback / constructor 里
 *   this.attachShadow(...) 渲染内部 DOM）。这是 Web Components 标准模型——宿主层不应再额外
 *   attachShadow，否则会包一层无用的 shadow root，且无法直接把 props 设到 CE 实例上。
 *   因此本组件的隔离 = 「CE 类内部的 shadow root」+「CE 注册名的全局唯一性」。
 *
 * 错误处理：fetch 失败 / 脚本执行抛错 / 缺必需 props → 渲染错误占位（不向上抛）
 */
import React, { useEffect, useRef, useState } from 'react'
import { FileWarning } from '@/assets/icons'
import { apiClient } from '@/services/api/client'
import { loggers } from '@/utils/logger'

/** WebComponentCardHost 渲染指令 props（由 PageRenderer 从 contributes.pages.props 透传） */
export interface WebComponentCardHostProps {
  /** 插件 id（拼 /ext/{pluginId}{scriptPath} 用） */
  pluginId?: string
  /** 插件提供的 JS 资源路径（相对插件根，如 "/component.js"；前导 / 可选） */
  scriptPath?: string
  /** Custom Element 标签名（必须含连字符，如 "my-widget"） */
  tagName?: string
  /** 传给 Custom Element 实例的属性（作为 DOM property 直接赋值，支持对象） */
  props?: Record<string, unknown>
  /** 标题（无障碍标签，可选） */
  title?: string
}

/** 校验 tagName：Custom Element 名必须含连字符（HTML 规范要求） */
function isValidTagName(name: string): boolean {
  return typeof name === 'string' && name.includes('-') && /^[a-z][a-z0-9-]*$/i.test(name)
}

/**
 * 执行插件 JS 脚本（等价 eval，让 customElements.define 在全局生效）
 *
 * 用 new Function 而非直接 eval：避免严格模式 eval 的作用域泄漏（直接 eval 会读外层 let），
 * new Function 体在全局作用域执行，customElements / window 等全局可正常解析。
 */
function executeScript(script: string): void {
  // eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
  const fn = new Function(script)
  fn.call(window)
}

/**
 * 把 props 设到 CE 实例上（作为 DOM property 直接赋值）。
 * 标量也会被设置；对象/数组通过引用传递（同进程优势，无需序列化）。
 */
function applyProps(el: HTMLElement, props: Record<string, unknown> | undefined): void {
  if (!props) return
  for (const [key, value] of Object.entries(props)) {
    try {
      // 优先 set property（能承载对象、不会触发 attribute 解析）
      ;(el as unknown as Record<string, unknown>)[key] = value
    } catch {
      // 只读 property 赋值失败时静默（部分内置 property 不可写）
    }
  }
}

export function WebComponentCardHost({
  pluginId,
  scriptPath,
  tagName,
  props,
  title,
}: WebComponentCardHostProps): React.ReactNode {
  const [error, setError] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  // 记录本实例挂载的 CE 元素，用于卸载时清理
  const elementRef = useRef<HTMLElement | null>(null)

  // 缺必需参数：早失败（避免无效 fetch）
  const invalidConfig =
    !pluginId || !scriptPath || !tagName || !isValidTagName(tagName)

  useEffect(() => {
    if (invalidConfig) return // 渲染分支已展示占位

    let cancelled = false
    const tag = tagName as string

    async function load(): Promise<void> {
      try {
        // 已注册（如其它实例已加载脚本）→ 跳过 fetch，避免重复 define 抛错
        if (customElements.get(tag) === undefined) {
          const path = scriptPath as string
          const url = `/ext/${pluginId}${path.startsWith('/') ? '' : '/'}${path}`
          const res = await apiClient.get<string>(url, {
            responseType: 'text',
            transformResponse: [(d) => d],
          })
          if (cancelled) return
          const script = typeof res.data === 'string' ? res.data : String(res.data)
          executeScript(script)
          if (cancelled) return
        }

        // 此时 tagName 应已定义；挂载元素实例
        const host = containerRef.current
        if (!host) return
        // 若上一次未清理干净，先清掉（防御）
        if (elementRef.current && elementRef.current.isConnected) {
          elementRef.current.remove()
        }
        const el = document.createElement(tag) as HTMLElement
        applyProps(el, props)
        host.appendChild(el)
        elementRef.current = el
        setReady(true)
      } catch (e) {
        const message = (e as Error)?.message ?? String(e)
        loggers.websocket.warn(`[WebComponentCardHost] ${tag} 加载失败: ${message}`)
        if (!cancelled) setError(message)
      }
    }

    void load()
    return () => {
      cancelled = true
      // 卸载：移除元素，触发 CE 的 disconnectedCallback
      if (elementRef.current && elementRef.current.isConnected) {
        elementRef.current.remove()
      }
      elementRef.current = null
    }
    // props 变化时重挂载元素（重新 apply props）；tag/scriptPath 变化也重跑
  }, [pluginId, scriptPath, tagName, invalidConfig, props])

  // 配置错误：展示占位（不调 fetch）
  if (invalidConfig) {
    const missing: string[] = []
    if (!pluginId) missing.push('pluginId')
    if (!scriptPath) missing.push('scriptPath')
    if (!tagName || !isValidTagName(tagName)) missing.push('tagName')
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        <FileWarning className="mr-2 h-5 w-5" />
        WebComponent 配置不完整（缺少 {missing.join(' / ')}）
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        <FileWarning className="mr-2 h-5 w-5" />
        WebComponent 加载失败: {error}
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      className="h-full w-full"
      title={title ?? tagName}
      data-wc-host={tagName}
      data-ready={ready ? 'true' : 'false'}
    />
  )
}

export default WebComponentCardHost
