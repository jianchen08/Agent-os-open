/**
 * 路由顶层错误兜底（createRouter 根路由 errorElement）。
 *
 * 替代 React Router 默认开发者错误页（"Unexpected Application Error!"，
 * 无任何恢复按钮的整页死路）：路由渲染抛错且无更近错误边界时落这里，
 * 提供两条恢复出路——重启前端（整页 reload，状态彻底重置）与返回首页
 * （SPA 导航，错误态随路由切换清除，适合渲染脏数据类错误）。
 *
 * 渲染零外部依赖面（内联样式 + 常量文案，不碰 store/antd）：错误态下
 * 本组件再抛错只会级联白屏，依赖面必须收到最小。
 */

import { useEffect, type ReactNode } from 'react'
import { useNavigate, useRouteError } from 'react-router-dom'
import { ROUTES } from '@/constants/routes'
import { captureException } from '@/services/errorReporting'

export function RouteErrorFallback(): ReactNode {
  const error = useRouteError()
  const navigate = useNavigate()
  const message = error instanceof Error ? error.message : String(error)

  // 上报与既有 ErrorBoundary 同通道（captureException）；message 作依赖：
  // 错误对象引用在重渲染间不稳定，文本相同不重复上报。
  useEffect(() => {
    captureException(error instanceof Error ? error : new Error(message), {
      component: 'RouteErrorFallback',
      action: 'routeRenderError',
      source: 'frontend',
    })
  }, [error, message])

  return (
    <div
      data-testid="route-error-fallback"
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '24px',
        backgroundColor: 'hsl(var(--background))',
        color: 'hsl(var(--foreground))',
      }}
    >
      <div
        style={{
          maxWidth: 560,
          width: '100%',
          border: '1px solid hsl(var(--border))',
          borderRadius: 8,
          padding: 32,
          textAlign: 'center',
          backgroundColor: 'hsl(var(--card))',
        }}
      >
        <h2 style={{ margin: '0 0 8px', fontSize: 20 }}>页面出错了</h2>
        <p style={{ margin: '0 0 24px', color: 'hsl(var(--muted-foreground))' }}>
          界面渲染遇到异常，可重启前端恢复；重启后重新进入会话即可。
        </p>
        <div style={{ display: 'flex', gap: 12, justifyContent: 'center' }}>
          <button
            type="button"
            data-testid="route-error-reload"
            onClick={() => window.location.reload()}
            style={{
              padding: '10px 24px',
              backgroundColor: 'hsl(var(--primary))',
              color: 'hsl(var(--primary-foreground))',
              border: 'none',
              borderRadius: 6,
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            重启前端
          </button>
          <button
            type="button"
            data-testid="route-error-home"
            onClick={() => void navigate(ROUTES.HOME, { replace: true })}
            style={{
              padding: '10px 24px',
              backgroundColor: 'hsl(var(--muted))',
              color: 'hsl(var(--foreground))',
              border: '1px solid hsl(var(--border))',
              borderRadius: 6,
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            返回首页
          </button>
        </div>
        <details style={{ marginTop: 24, textAlign: 'left' }}>
          <summary style={{ cursor: 'pointer', color: 'hsl(var(--muted-foreground))', fontSize: 13 }}>
            错误详情
          </summary>
          <pre
            data-testid="route-error-message"
            style={{
              marginTop: 8,
              padding: 12,
              backgroundColor: 'hsl(var(--muted))',
              borderRadius: 4,
              overflow: 'auto',
              fontSize: 12,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {message}
          </pre>
        </details>
      </div>
    </div>
  )
}
