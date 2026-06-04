/**
 * 工具卡片渲染配置注册表
 *
 * 为不同工具的活动卡片提供差异化的渲染和交互配置。
 * 每个工具可以配置：自定义图标、标题格式化、详情区块、操作按钮。
 *
 * @module toolCardRegistry
 */

import { Copy, FileText, Globe, Target, Terminal, Trash2 } from 'lucide-react'
import type { ActivityAction, ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import type { ReactNode } from 'react'

/**
 * 工具卡片渲染配置
 */
export interface ToolCardConfig {
  /** 工具名称 */
  name: string
  /** 自定义图标 */
  icon?: ReactNode
  /** 自定义标题（传入 toolCall 参数，返回显示标题） */
  formatTitle?: (toolCall: MessageToolCall) => string
  /** 构建详情区块（传入 toolCall，返回详情区块列表） */
  buildDetails?: (toolCall: MessageToolCall) => ActivityDetailBlock[]
  /** 构建操作按钮（传入 toolCall，返回操作按钮列表） */
  buildActions?: (toolCall: MessageToolCall) => ActivityAction[]
  /** 自定义样式类名 */
  className?: string
  /** 运行状态的自定义颜色（CSS 色值），用于区分阻塞等待用户的工具 */
  runningColor?: string
  /** 是否关联文件（为 true 时自动从参数中提取文件路径） */
  hasFilePath?: boolean
}

/**
 * 注册表：工具名 → 渲染配置
 */
const registry = new Map<string, ToolCardConfig>()

/** 全局文件打开回调（支持 containerTaskId） */
let globalOnOpenFile: ((filePath: string, containerTaskId?: string) => void | Promise<void>) | null = null

/**
 * 注册全局文件打开回调
 *
 * 在应用启动时调用一次，用于设置文件打开的统一处理逻辑。
 *
 * @param callback - 文件打开回调函数
 */
export function registerGlobalOpenFileCallback(
  callback: (filePath: string, containerTaskId?: string) => void | Promise<void>,
): void {
  globalOnOpenFile = callback
}

/**
 * 获取全局文件打开回调
 */
export function getGlobalOpenFileCallback(): (filePath: string, containerTaskId?: string) => void | Promise<void> {
  return globalOnOpenFile || ((filePath: string, _containerTaskId?: string) => {
    console.warn('[toolCardRegistry] 未注册文件打开回调，请在应用启动时调用 registerGlobalOpenFileCallback')
  })
}

/**
 * 注册工具卡片配置
 */
export function registerToolCard(config: ToolCardConfig): void {
  registry.set(config.name, config)
}

/**
 * 获取工具卡片配置
 */
export function getToolCardConfig(toolName: string): ToolCardConfig | undefined {
  return registry.get(toolName)
}

/**
 * 使用工具配置增强 ActivityData
 *
 * 在 toolCallToActivity 转换后调用，用工具配置覆盖/增强默认渲染
 */
export function enhanceActivityWithToolConfig(
  activity: ActivityData,
  toolCall: MessageToolCall,
  options?: {
    onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
  },
): ActivityData {
  if (activity.type !== 'tool_call' || !activity.toolName) {
    return activity
  }

  const config = getToolCardConfig(activity.toolName)
  if (!config) {
    return activity
  }

  const enhanced = { ...activity }

  if (config.formatTitle) {
    enhanced.title = config.formatTitle(toolCall)
  }

  if (config.buildDetails) {
    enhanced.details = config.buildDetails(toolCall)
  }

  if (config.buildActions) {
    enhanced.actions = config.buildActions(toolCall)
  }

  if (config.icon) {
    enhanced.customIcon = config.icon
  }

  if (config.className) {
    enhanced.customClassName = config.className
  }

  if (config.runningColor) {
    enhanced.customColor = config.runningColor
  }

  // 自动提取文件路径并注入打开文件回调（携带 containerTaskId）
  if (config.hasFilePath) {
    const filePath = extractFilePath(toolCall)
    if (filePath) {
      enhanced.filePath = filePath
      const openFileCallback = options?.onOpenFile || getGlobalOpenFileCallback()
      const taskId = toolCall.containerTaskId
      enhanced.onOpenFile = () => openFileCallback(filePath, taskId)
    }
  }

  return enhanced
}

function buildDefaultDetails(toolCall: MessageToolCall): ActivityDetailBlock[] {
  const details: ActivityDetailBlock[] = []

  details.push({
    id: 'args',
    label: '参数',
    content: toolCall.tool_args,
    contentType: 'json',
    collapsible: true,
    defaultExpanded: false,
  })

  if (toolCall.result !== undefined && toolCall.result !== null) {
    details.push({
      id: 'result',
      label: '结果',
      content: toolCall.result as string | Record<string, unknown>,
      contentType: 'json',
      collapsible: true,
      defaultExpanded: false,
    })
  }

  if (toolCall.partialOutput && toolCall.partialOutput.length > 0) {
    details.push({
      id: 'output',
      label: '执行输出',
      content: toolCall.partialOutput.join('\n'),
      contentType: 'text',
      collapsible: false,
    })
  }

  return details
}

function buildDefaultActions(toolCall: MessageToolCall): ActivityAction[] {
  const actions: ActivityAction[] = [
    {
      id: 'copy_args',
      icon: <Copy className="h-3.5 w-3.5" />,
      label: '复制参数',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(JSON.stringify(toolCall.tool_args, null, 2))
      },
    },
  ]

  if (toolCall.result !== undefined) {
    actions.push({
      id: 'copy_result',
      icon: <Copy className="h-3.5 w-3.5" />,
      label: '复制结果',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(
          typeof toolCall.result === 'string'
            ? toolCall.result
            : JSON.stringify(toolCall.result, null, 2),
        )
      },
    })
  }

  return actions
}

function extractFilePath(toolCall: MessageToolCall): string {
  const args = toolCall.tool_args as Record<string, unknown> | null
  if (!args) return ''
  return (args.file_path as string) || (args.path as string) || ''
}

function extractCommand(toolCall: MessageToolCall): string {
  const args = toolCall.tool_args as Record<string, unknown> | null
  if (!args) return ''
  return (args.command as string) || (args.cmd as string) || ''
}

function extractUrl(toolCall: MessageToolCall): string {
  const args = toolCall.tool_args as Record<string, unknown> | null
  if (!args) return ''
  return (args.url as string) || (args.query as string) || ''
}

/**
 * 安全解析可能是 Python dict 字符串的结果
 *
 * 处理 tc.result 可能的多种格式：
 * 1. 已经是对象 → 直接返回
 * 2. 标准 JSON 字符串 → JSON.parse 解析
 * 3. Python dict 字符串（单引号、True/False/None）→ 替换后解析
 * 4. 解析失败 → 返回 null
 *
 * @param result - 工具调用的返回结果，可能是对象或字符串
 * @returns 解析后的对象，或解析失败时返回 null
 */
export function safeParseResult(result: unknown): Record<string, unknown> | null {
  // 已经是对象，直接返回
  if (result !== null && result !== undefined && typeof result === 'object') {
    return result as Record<string, unknown>
  }

  // 非字符串无法解析
  if (typeof result !== 'string') {
    return null
  }

  const str = result.trim()
  if (!str) return null

  // 第一次尝试：标准 JSON 解析
  try {
    const parsed = JSON.parse(str)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch {
    // 不是标准 JSON，继续尝试 Python dict 格式
  }

  // 第二次尝试：Python dict 格式（单引号 → 双引号，True/False/None → JSON 值）
  try {
    let normalized = str
    // 将 Python 布尔值和 None 替换为 JSON 兼容值
    normalized = normalized.replace(/\bTrue\b/g, 'true')
    normalized = normalized.replace(/\bFalse\b/g, 'false')
    normalized = normalized.replace(/\bNone\b/g, 'null')
    // 将单引号替换为双引号（注意：这只是简单替换，对嵌套引号场景可能有局限）
    normalized = normalized.replace(/'/g, '"')
    const parsed = JSON.parse(normalized)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch {
    // Python dict 格式也解析失败
  }

  return null
}

registerToolCard({
  name: 'file_read',
  icon: <FileText className="h-4 w-4" />,
  hasFilePath: true,
  formatTitle: (tc) => {
    const path = extractFilePath(tc)
    const fileName = path ? path.split(/[/\\]/).pop() || path : tc.tool_name
    return `读取 ${fileName}`
  },
  buildDetails: (tc) => {
    const path = extractFilePath(tc)
    const details: ActivityDetailBlock[] = []

    if (path) {
      details.push({
        id: 'filepath',
        label: '文件路径',
        content: path,
        contentType: 'code',
        collapsible: false,
      })
    }

    if (tc.result !== undefined && tc.result !== null) {
      const resultStr =
        typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
      details.push({
        id: 'result',
        label: '文件内容',
        content: resultStr,
        contentType: 'code',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    return details
  },
  buildActions: (tc) => {
    const actions: ActivityAction[] = []

    if (tc.result !== undefined) {
      actions.push({
        id: 'copy_content',
        icon: <Copy className="h-3.5 w-3.5" />,
        label: '复制内容',
        type: 'copy',
        onClick: () => {
          const content =
            typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
          navigator.clipboard.writeText(content)
        },
      })
    }

    return actions
  },
})

registerToolCard({
  name: 'file_write',
  icon: <Trash2 className="h-4 w-4" />,
  hasFilePath: true,
  formatTitle: (tc) => {
    const path = extractFilePath(tc)
    const fileName = path ? path.split(/[/\\]/).pop() || path : tc.tool_name
    return `写入 ${fileName}`
  },
  buildDetails: (tc) => {
    const path = extractFilePath(tc)
    const details: ActivityDetailBlock[] = []

    if (path) {
      details.push({
        id: 'filepath',
        label: '文件路径',
        content: path,
        contentType: 'code',
        collapsible: false,
      })
    }

    const args = tc.tool_args as Record<string, unknown> | null
    if (args?.content) {
      const contentStr =
        typeof args.content === 'string' ? args.content : JSON.stringify(args.content, null, 2)
      details.push({
        id: 'content',
        label: '写入内容',
        content: contentStr,
        contentType: 'code',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    return details
  },
  buildActions: buildDefaultActions,
})

registerToolCard({
  name: 'bash_execute',
  icon: <Terminal className="h-4 w-4" />,
  formatTitle: (tc) => {
    const cmd = extractCommand(tc)
    if (cmd) {
      const firstLine = cmd.split('\n')[0].trim()
      return firstLine.length > 60 ? firstLine.slice(0, 57) + '...' : firstLine
    }
    return '执行命令'
  },
  buildDetails: (tc) => {
    const cmd = extractCommand(tc)
    const details: ActivityDetailBlock[] = []

    if (cmd) {
      details.push({
        id: 'command',
        label: '命令',
        content: cmd,
        contentType: 'code',
        language: 'bash',
        collapsible: true,
        defaultExpanded: true,
      })
    }

    if (tc.result !== undefined && tc.result !== null) {
      const resultStr =
        typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
      details.push({
        id: 'result',
        label: '输出',
        content: resultStr,
        contentType: 'code',
        language: 'text',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    if (tc.error) {
      details.push({
        id: 'error',
        label: '错误',
        content: tc.error,
        contentType: 'text',
        collapsible: true,
        defaultExpanded: true,
      })
    }

    return details
  },
  buildActions: (tc) => {
    const actions: ActivityAction[] = []

    const cmd = extractCommand(tc)
    if (cmd) {
      actions.push({
        id: 'copy_cmd',
        icon: <Copy className="h-3.5 w-3.5" />,
        label: '复制命令',
        type: 'copy',
        onClick: () => navigator.clipboard.writeText(cmd),
      })
    }

    if (tc.result !== undefined) {
      actions.push({
        id: 'copy_output',
        icon: <Copy className="h-3.5 w-3.5" />,
        label: '复制输出',
        type: 'copy',
        onClick: () => {
          const content =
            typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
          navigator.clipboard.writeText(content)
        },
      })
    }

    return actions
  },
})

registerToolCard({
  name: 'web_search',
  icon: <Globe className="h-4 w-4" />,
  formatTitle: (tc) => {
    const query = extractUrl(tc)
    if (query) {
      return query.length > 50 ? query.slice(0, 47) + '...' : query
    }
    return '网页搜索'
  },
  buildDetails: (tc) => {
    const query = extractUrl(tc)
    const details: ActivityDetailBlock[] = []

    if (query) {
      details.push({
        id: 'query',
        label: '搜索内容',
        content: query,
        contentType: 'text',
        collapsible: false,
      })
    }

    if (tc.result !== undefined && tc.result !== null) {
      const resultStr =
        typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
      details.push({
        id: 'result',
        label: '搜索结果',
        content: resultStr,
        contentType: 'text',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    return details
  },
  buildActions: buildDefaultActions,
})

registerToolCard({
  name: 'fetch',
  icon: <Globe className="h-4 w-4" />,
  formatTitle: (tc) => {
    const url = extractUrl(tc)
    if (url) {
      try {
        const hostname = new URL(url.startsWith('http') ? url : `https://${url}`).hostname
        return `访问 ${hostname}`
      } catch {
        return `访问网页`
      }
    }
    return '访问网页'
  },
  buildDetails: (tc) => {
    const url = extractUrl(tc)
    const details: ActivityDetailBlock[] = []

    if (url) {
      details.push({
        id: 'url',
        label: 'URL',
        content: url,
        contentType: 'code',
        collapsible: false,
      })
    }

    if (tc.result !== undefined && tc.result !== null) {
      const resultStr =
        typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
      const isLong = resultStr.length > 500
      details.push({
        id: 'result',
        label: '页面内容',
        content: isLong ? resultStr.slice(0, 500) + '\n\n... (内容已截断)' : resultStr,
        contentType: 'text',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    return details
  },
  buildActions: buildDefaultActions,
})

/**
 * task_submit 工具卡片配置
 *
 * 显示任务提交的目标、描述、执行者及提交结果
 */
registerToolCard({
  name: 'task_submit',
  icon: <Target className="h-4 w-4" />,
  formatTitle: (tc) => {
    const args = tc.tool_args as Record<string, unknown> | null
    const goal = args?.goal as Record<string, unknown> | null
    const title = (goal?.title as string) || (args?.description as string) || '任务提交'
    return `提交任务: ${title}`
  },
  buildDetails: (tc) => {
    const args = tc.tool_args as Record<string, unknown> | null
    const details: ActivityDetailBlock[] = []

    // 目标信息
    const goal = args?.goal as Record<string, unknown> | null
    if (goal?.title) {
      details.push({
        id: 'goal',
        label: '任务目标',
        content: goal.title as string,
        contentType: 'text',
        collapsible: false,
      })
    }
    if (goal?.description) {
      details.push({
        id: 'goal_desc',
        label: '详细描述',
        content: goal.description as string,
        contentType: 'text',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    // 执行者信息
    const targetId = args?.target_id as string
    if (targetId) {
      details.push({
        id: 'target',
        label: '执行者',
        content: targetId,
        contentType: 'text',
        collapsible: false,
      })
    }

    // 提交结果：安全解析 Python dict 字符串格式的 result
    if (tc.result !== undefined && tc.result !== null) {
      const parsedResult = safeParseResult(tc.result)

      if (parsedResult) {
        // 解析成功，从 output 字段中提取任务数据
        const output = parsedResult.output as Record<string, unknown> | undefined
        const taskId = (output?.task_id as string) || (parsedResult.task_id as string) || ''
        const status = (output?.status as string) || (parsedResult.status as string) || ''
        const message = (output?.message as string) || (parsedResult.message as string) || ''
        const title = (output?.title as string) || (parsedResult.title as string) || ''

        const contentParts: string[] = []
        if (taskId) contentParts.push(`任务ID: ${taskId}`)
        if (status) contentParts.push(`状态: ${status}`)
        if (title) contentParts.push(`标题: ${title}`)
        if (message) contentParts.push(message)

        details.push({
          id: 'result',
          label: '提交结果',
          content: contentParts.length > 0 ? contentParts.join('\n') : '提交成功',
          contentType: 'text',
          collapsible: true,
          defaultExpanded: false,
        })
      } else {
        // 解析失败，直接展示原始字符串
        const resultStr =
          typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
        details.push({
          id: 'result',
          label: '提交结果',
          content: resultStr,
          contentType: 'text',
          collapsible: true,
          defaultExpanded: false,
        })
      }
    }

    return details
  },
  buildActions: buildDefaultActions,
})

/**
 * human_interaction 工具卡片配置
 *
 * 运行状态使用主色（primary），视觉上区分阻塞等待用户的交互工具与普通执行工具
 */
registerToolCard({
  name: 'human_interaction',
  runningColor: 'hsl(var(--primary))',
})

export default registry
