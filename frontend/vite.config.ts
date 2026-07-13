/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import path from 'path'
import { defineConfig, loadEnv } from 'vite'

/**
 * Vite 构建配置
 * 基于 Vite + React + TypeScript 模板，配置路径别名、代理和构建优化
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = process.env.VITE_API_BASE_URL || env.VITE_API_BASE_URL || ''

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5289,
      strictPort: false,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/ws': {
          target: apiTarget,
          changeOrigin: true,
          ws: true,
        },
        '/media': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/uploads': {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
        '@/components': path.resolve(__dirname, './src/components'),
        '@/pages': path.resolve(__dirname, './src/pages'),
        '@/stores': path.resolve(__dirname, './src/stores'),
        '@/services': path.resolve(__dirname, './src/services'),
        '@/types': path.resolve(__dirname, './src/types'),
        '@/utils': path.resolve(__dirname, './src/utils'),
        '@/hooks': path.resolve(__dirname, './src/hooks'),
        '@/constants': path.resolve(__dirname, './src/constants'),
        '@/assets': path.resolve(__dirname, './src/assets'),
      },
    },
    build: {
      cssCodeSplit: true,
      chunkSizeWarningLimit: 500,
      minify: 'esbuild',
      target: 'es2015',
      modulePreload: true,
    },
    esbuild: {
      drop: process.env.NODE_ENV === 'production' ? ['debugger'] : [],
    },
    optimizeDeps: {
      include: [
        'react',
        'react-dom',
        'react-router-dom',
        'zustand',
        'axios',
        'lucide-react',
      ],
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: './src/test/setup.ts',
      include: ['src/**/*.test.{ts,tsx}'],
      exclude: ['node_modules', 'dist'],
      testTimeout: 10000,
      hookTimeout: 10000,
      reporters: ['default'],
      watch: false,
      sequence: {
        shuffle: false,
      },
      fileParallelism: false,
      css: false,
    },
  }
})
