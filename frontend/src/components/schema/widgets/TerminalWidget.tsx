/**
 * TerminalWidget —— 终端组件「插件接入点」骨架
 *
 * 设计原则（见任务说明）：本组件不实现真实终端 PTY，也不引入 xterm.js。
 * 它是一个「接入点」：
 * - 声明 pluginId 时，把渲染委托给插件（插件通过 webview 提供 iframe 终端 UI，
 *   宿主在此容器内挂载）。
 * - 未声明 pluginId 时，渲染占位提示，引导接入 pluginId / connector。
 *
 * 接入路径：WebviewWidget 通道——插件提供 webview URL，
 * 本组件在挂载点渲染 iframe。
 *   （本骨架仅预留挂载点容器，不实例化 xterm / iframe；完整实现见后续 Phase。）
 *
 * Props 契约（flat，与 TableWidget 一致；由 registerWidgets 以
 * `WidgetComponent = ComponentType<Record<string, unknown>>` 注册）：
 *   { pluginId?, terminalId?, dataSource?, title?, cols?, rows?, ...rest }
 */

/** TerminalWidget props（flat，与 WidgetRegistry 的 WidgetProps 契约一致）。 */
export interface TerminalWidgetProps {
  /** 提供终端实现的插件 id（接入后委托渲染） */
  pluginId?: string
  /** 终端实例 id（多终端场景区分；透传给插件） */
  terminalId?: string
  /** 数据源 URI（如 `terminal://session-xxx`；透传给插件，骨架不做拉取） */
  dataSource?: string
  /** 兼容 composer 传入的 snake_case */
  data_source?: string
  /** 标题（无障碍 / 头部展示，可选） */
  title?: string
  /** 列数（透传给插件终端，骨架不强制） */
  cols?: number
  /** 行数（透传给插件终端，骨架不强制） */
  rows?: number
}

/**
 * 终端组件骨架（插件接入点）
 *
 * @param props - flat 组件属性
 * @returns 委托插件渲染的容器，或未接入时的占位提示
 */
export function TerminalWidget(props: TerminalWidgetProps) {
  const {
    pluginId,
    terminalId,
    dataSource,
    data_source,
    title,
    cols,
    rows,
  } = props

  const resolvedDataSource = dataSource ?? data_source

  // 未声明 pluginId → 占位提示，引导接入
  if (!pluginId) {
    return (
      <div
        data-testid="terminal-placeholder"
        className="text-muted-foreground flex h-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed p-8 text-center text-sm"
      >
        <span className="text-base font-medium">终端组件（未接入）</span>
        <span>
          接入 <code className="bg-muted/60 rounded px-1 py-0.5 font-mono text-xs">pluginId</code>{' '}
          或 connector 以启用终端
        </span>
        {terminalId && (
          <span className="text-xs opacity-70">terminalId: {terminalId}</span>
        )}
      </div>
    )
  }

  // 声明了 pluginId → 渲染插件接入容器
  // 这里只预留挂载点；将来插件提供的 webview iframe 将注入到
  // `terminal-plugin-mount` 容器内（与 WebviewWidget 同模型）。
  return (
    <div
      data-testid="terminal-plugin-host"
      data-plugin-id={pluginId}
      className="flex h-full w-full flex-col rounded-lg border"
    >
      <div className="border-b bg-muted/40 flex items-center justify-between px-3 py-1.5">
        <span className="text-foreground text-xs font-medium">
          {title ?? '终端'} · 由插件「{pluginId}」提供
        </span>
        {(cols || rows) && (
          <span className="text-muted-foreground text-[10px]">
            {cols ? `${cols}c` : ''}
            {cols && rows ? ' × ' : ''}
            {rows ? `${rows}r` : ''}
          </span>
        )}
      </div>
      <div
        data-testid="terminal-plugin-mount"
        data-terminal-id={terminalId}
        data-data-source={resolvedDataSource}
        className="bg-terminal-plugin-mount relative flex-1 overflow-hidden"
        aria-label={`终端由插件 ${pluginId} 提供`}
      >
        {/* 真实终端 UI（xterm / CE / iframe）由插件在此挂载点注入 */}
        <div className="text-muted-foreground absolute inset-0 flex items-center justify-center text-xs opacity-60">
          等待插件「{pluginId}」注入终端 UI…
        </div>
      </div>
    </div>
  )
}

export default TerminalWidget
