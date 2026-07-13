/**
 * 错误边界组件
 *
 * 捕获子组件树中的 JavaScript 错误，显示备用 UI
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'
import { captureException } from '@/services/errorReporting'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
  errorInfo: ErrorInfo | null
}

/**
 * 错误边界组件类
 */
export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
    }
  }

  static getDerivedStateFromError(error: Error): State {
    // 更新 state 使下一次渲染能够显示降级后的 UI
    return {
      hasError: true,
      error,
      errorInfo: null,
    }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // 将错误日志上报给错误追踪系统
    captureException(error, {
      component: 'ErrorBoundary',
      componentStack: errorInfo.componentStack,
    })

    this.setState({
      error,
      errorInfo,
    })
  }

  render(): ReactNode {
    if (this.state.hasError) {
      // 自定义降级 UI
      return (
        <div
          style={{
            padding: '20px',
            margin: '50px auto',
            maxWidth: '800px',
            backgroundColor: 'hsl(var(--background))',
            border: '1px solid hsl(var(--border))',
            borderRadius: '4px',
            color: 'hsl(var(--foreground))',
          }}
        >
          <h2 style={{ color: 'hsl(var(--foreground))' }}>⚠️ 出错了</h2>
          <p style={{ color: 'hsl(var(--muted-foreground))' }}>应用程序遇到了一个错误。请刷新页面重试。</p>
          {this.state.error && (
            <details style={{ marginTop: '20px' }}>
              <summary style={{ cursor: 'pointer', marginBottom: '10px' }}>
                错误详情（开发者用）
              </summary>
              <pre
                style={{
                  backgroundColor: 'hsl(var(--muted))',
                  padding: '10px',
                  borderRadius: '4px',
                  overflow: 'auto',
                  fontSize: '12px',
                  color: 'hsl(var(--foreground))',
                }}
              >
                {this.state.error.toString()}
                {this.state.errorInfo?.componentStack}
              </pre>
            </details>
          )}
          <button
            onClick={() => window.location.reload()}
            style={{
              marginTop: '20px',
              padding: '10px 20px',
              backgroundColor: 'hsl(var(--primary))',
              color: 'hsl(var(--primary-foreground))',
              border: 'none',
              borderRadius: '4px',
              cursor: 'pointer',
            }}
          >
            刷新页面
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
