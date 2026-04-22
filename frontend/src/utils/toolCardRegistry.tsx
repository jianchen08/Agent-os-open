/**
 * 工具卡片渲染配置注册表
 *
 * 为不同工具的活动卡片提供差异化的渲染和交互配置。
 * 每个工具可以配置：自定义图标、标题格式化、详情区块、操作按钮。
 *
 * @module toolCardRegistry
 */

import type {
  ActivityAction,
  ActivityData,
  ActivityDetailBlock,
} from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import {
  Copy,
  FileText,
  Globe,
  Terminal,
  Trash2,
} from 'lucide-react'
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
}

/**
 * 注册表：工具名 → 渲染配置
 */
const registry = new Map<string, ToolCardConfig>()

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
  toolCall: MessageToolCall
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
    defaultExpanded: true,
  })

  if (toolCall.result !== undefined && toolCall.result !== null) {
    details.push({
      id: 'result',
      label: '结果',
      content: toolCall.result as string | Record<string, unknown>,
      contentType: 'json',
      collapsible: true,
      defaultExpanded: true,
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
      icon: <Copy className="w-3.5 h-3.5" />,
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
      icon: <Copy className="w-3.5 h-3.5" />,
      label: '复制结果',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(
          typeof toolCall.result === 'string'
            ? toolCall.result
            : JSON.stringify(toolCall.result, null, 2)
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

registerToolCard({
  name: 'file_read',
  icon: <FileText className="w-4 h-4" />,
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
      const resultStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
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
        icon: <Copy className="w-3.5 h-3.5" />,
        label: '复制内容',
        type: 'copy',
        onClick: () => {
          const content = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
          navigator.clipboard.writeText(content)
        },
      })
    }

    return actions
  },
})

registerToolCard({
  name: 'file_write',
  icon: <Trash2 className="w-4 h-4" />,
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
      const contentStr = typeof args.content === 'string' ? args.content : JSON.stringify(args.content, null, 2)
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
  icon: <Terminal className="w-4 h-4" />,
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
      const resultStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
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
        icon: <Copy className="w-3.5 h-3.5" />,
        label: '复制命令',
        type: 'copy',
        onClick: () => navigator.clipboard.writeText(cmd),
      })
    }

    if (tc.result !== undefined) {
      actions.push({
        id: 'copy_output',
        icon: <Copy className="w-3.5 h-3.5" />,
        label: '复制输出',
        type: 'copy',
        onClick: () => {
          const content = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
          navigator.clipboard.writeText(content)
        },
      })
    }

    return actions
  },
})

registerToolCard({
  name: 'web_search',
  icon: <Globe className="w-4 h-4" />,
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
      const resultStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
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
  icon: <Globe className="w-4 h-4" />,
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
      const resultStr = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2)
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

export default registry
