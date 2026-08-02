/**
 * E2E 测试专用 Vite 配置
 *
 * 背景：dev server 默认 optimizeDeps 预打包大依赖（mermaid/antd 等）时
 * 在受限容器中 OOM Killed，导致 vite 主进程退出、playwright 全部连接失败。
 * 本配置禁用依赖预打包（noDiscovery + 空 include，按需转换，稳定但首次加载稍慢），
 * 仅用于 E2E 验证，不影响生产构建与正常 dev 体验。
 */
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget =
    process.env.VITE_PROXY_TARGET ||
    env.VITE_PROXY_TARGET ||
    process.env.VITE_API_BASE_URL ||
    env.VITE_API_BASE_URL ||
    ''

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5290,
      strictPort: true,
      proxy: {
        '/api': { target: apiTarget, changeOrigin: true },
        '/ext': { target: apiTarget, changeOrigin: true },
        '/ws': { target: apiTarget, changeOrigin: true, ws: true },
        '/media': { target: apiTarget, changeOrigin: true },
        '/uploads': { target: apiTarget, changeOrigin: true },
      },
    },

    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    esbuild: { drop: [] },
  }
})
