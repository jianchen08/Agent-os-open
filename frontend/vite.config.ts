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
  const apiTarget = process.env.VITE_PROXY_TARGET || env.VITE_PROXY_TARGET || process.env.VITE_API_BASE_URL || env.VITE_API_BASE_URL || ''

  return {
    plugins: [
      react(),
      // 强制回退 esbuild：@vitejs/plugin-react 的 config hook 会强制设置 oxc（Vite 8
      // 默认转换器），导致 CJS interop 缺陷（"does not provide an export named 'default'"）
      // + cgroup 1.32GB 内存下 OOM。本 plugin 的 config hook 在 react() 之后执行，
      // 后面的 config 覆盖前面的，将 oxc 关掉回退 esbuild（内存低 + CJS interop 成熟）。
      {
        name: 'force-esbuild-transformer',
        config() {
          return { oxc: false }
        },
      },
    ],
    server: {
      host: '0.0.0.0',
      // 6390：避开 container_22404 的 5289/5290/6290。CLI 通过 --port 覆盖；此处为 vite 默认。
      port: 6390,
      strictPort: false,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/ext': {
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
        // antd icons 的 CJS asn 子路径（@ant-design/icons-svg/lib/asn/*）改为 ESM 版
        // （@ant-design/icons-svg/es/asn/*）：ESM 可被浏览器直接加载，无需在
        // optimizeDeps.include 中逐个预构建 847 项 CJS asn，显著降低 dev 启动预构建
        // 时间与"每次启动都重新做"的概率。
        // DEBT: alias 写死了 @ant-design/icons-svg 的 CJS 子目录名（lib/asn）改向 ESM
        // （es/asn），以减少 noDiscovery 下 847 项预构建。ceiling: 依赖 antd/icons-svg
        // 4.x 保持 lib/asn + es/asn 对称结构；一旦上游移除 CJS 入口或改名，本 alias 失效。
        // upgrade: 评估切换到 babel-plugin-import / unplugin 等更稳定按需方案时移除本 alias。
        '@ant-design/icons-svg/lib/asn': path.resolve(
          __dirname,
          'node_modules/@ant-design/icons-svg/es/asn',
        ),
      },
    },
    build: {
      cssCodeSplit: true,
      chunkSizeWarningLimit: 500,
      minify: 'esbuild',
      target: 'es2015',
      modulePreload: true,
      rollupOptions: {
        output: {
          // 合理分包：按依赖域拆分 vendor chunk，避免所有第三方库挤进单个大 chunk
          // （首屏 JS 过大 → 下载/解析慢）。登录页/壳层不依赖聊天重库
          // （@lobehub/ui、mermaid、highlight.js 等），分包后首屏只加载轻量 chunk。
          manualChunks(id) {
            if (!id.includes('node_modules')) return undefined
            if (id.includes('@lobehub') || id.includes('@emoji-mart')) return 'vendor-lobehub'
            if (id.includes('mermaid') || id.includes('d3-') || id.includes('cytoscape')) return 'vendor-mermaid'
            if (id.includes('highlight.js') || id.includes('lowlight') || id.includes('prismjs')) return 'vendor-syntax'
            if (id.includes('react-syntax-highlighter')) return 'vendor-syntax'
            if (id.includes('antd') || id.includes('@ant-design') || id.includes('/rc-') || id.includes('@rc-component')) return 'vendor-antd'
            if (id.includes('lucide-react')) return 'vendor-icons'
            if (id.includes('/react/') || id.includes('react-dom') || id.includes('scheduler')) return 'vendor-react'
            return 'vendor'
          },
        },
      },
    },
    esbuild: {
      drop: process.env.NODE_ENV === 'production' ? ['debugger'] : [],
    },
    // Vite 8 默认使用 oxc 转换器：CJS interop 处理有缺陷（浏览器端报
    // "does not provide an export named 'default'"），且内存占用大（cgroup 1.32GB 下 OOM）。
    // oxc: false 回退 esbuild：内存占用低 + CJS interop 成熟，解决双重问题。
    // 注意：@vitejs/plugin-react 的 config hook 会强制设置 oxc，因此需配合
    // 上方 force-esbuild-transformer 插件（config hook 后执行覆盖）才能生效。
    oxc: false,
    optimizeDeps: {
      // esbuild + noDiscovery + include 白名单（268 基础已验证成功 667 deps）
      // + antd 动态 icon 846 子路径全量（根治 @ant-design/icons-svg/lib/asn interop）
      // 注意：noDiscovery 下白名单必须覆盖应用实际加载的 UI 栈，否则浏览器会逐文件
      // 加载 node_modules 原始源码。曾漏掉 @lobehub/ui / antd / mermaid 等 → 冷启动
      // 6600+ 请求（@lobehub/icons 2200+、antd 647、mermaid+d3 700…），首屏 7-8 秒。
      // 预构建后压缩到 ~1300 请求、首屏 ~1.5s。子路径（antd/es/*、@pierre/diffs/react、
      // react-syntax-highlighter/dist/*）单独列出，noDiscovery 不会自动发现它们。
      noDiscovery: true,
      include: [
        '@ant-design/colors',
        // —— 应用 UI 栈（不预构建 → 逐文件加载 → 首屏 7-8s）——
        'antd',
        'antd/es/splitter',
        'antd-style',
        '@lobehub/ui',
        '@lobehub/icons',
        '@lobehub/fluent-emoji',
        '@pierre/diffs',
        '@pierre/diffs/react',
        'mermaid',
        'react-markdown',
        'remark-gfm',
        'react-syntax-highlighter/dist/esm/styles/prism',
        '@ant-design/cssinjs',
        '@ant-design/cssinjs-utils',
        '@ant-design/fast-color',
        '@ant-design/icons',
        '@ant-design/icons-svg',
        // 注：不再逐个预构建 @ant-design/icons-svg/lib/asn/*（原 847 项 CJS 子路径）。
        // resolve.alias 已将该前缀重定向到 es/asn（ESM 版），浏览器可直接加载，
        // 免除预构建，从根上消除"optimizeDeps.include 1114 项 / 每次启动重新预构建"。
        '@ant-design/react-slick',
        '@babel/code-frame',
        '@babel/generator',
        '@babel/helper-module-imports',
        '@babel/helper-string-parser',
        '@babel/helper-validator-identifier',
        '@babel/parser',
        '@babel/runtime/regenerator',
        '@babel/template',
        '@babel/traverse',
        '@babel/types',
        '@base-ui/react',
        '@braintree/sanitize-url',
        '@dnd-kit/accessibility',
        '@dnd-kit/core',
        '@dnd-kit/modifiers',
        '@dnd-kit/sortable',
        '@dnd-kit/utilities',
        '@emoji-mart/data',
        '@emotion/babel-plugin',
        '@emotion/cache',
        '@emotion/css',
        '@emotion/hash',
        '@emotion/is-prop-valid',
        '@emotion/memoize',
        '@emotion/react',
        '@emotion/serialize',
        '@emotion/sheet',
        '@emotion/unitless',
        '@emotion/use-insertion-effect-with-fallbacks',
        '@emotion/utils',
        '@emotion/weak-memoize',
        '@floating-ui/core',
        '@floating-ui/dom',
        '@floating-ui/react',
        '@floating-ui/react-dom',
        '@floating-ui/utils',
        '@jridgewell/gen-mapping',
        '@jridgewell/resolve-uri',
        '@jridgewell/sourcemap-codec',
        '@jridgewell/trace-mapping',
        '@pierre/theme',
        '@primer/octicons',
        '@radix-ui/number',
        '@radix-ui/primitive',
        '@radix-ui/react-arrow',
        '@radix-ui/react-collection',
        '@radix-ui/react-compose-refs',
        '@radix-ui/react-context',
        '@radix-ui/react-dialog',
        '@radix-ui/react-direction',
        '@radix-ui/react-dismissable-layer',
        '@radix-ui/react-dropdown-menu',
        '@radix-ui/react-focus-guards',
        '@radix-ui/react-focus-scope',
        '@radix-ui/react-id',
        '@radix-ui/react-menu',
        '@radix-ui/react-popper',
        '@radix-ui/react-portal',
        '@radix-ui/react-presence',
        '@radix-ui/react-primitive',
        '@radix-ui/react-roving-focus',
        '@radix-ui/react-select',
        '@radix-ui/react-slot',
        '@radix-ui/react-tooltip',
        '@radix-ui/react-use-callback-ref',
        '@radix-ui/react-use-controllable-state',
        '@radix-ui/react-use-effect-event',
        '@radix-ui/react-use-is-hydrated',
        '@radix-ui/react-use-layout-effect',
        '@radix-ui/react-use-previous',
        '@radix-ui/react-use-rect',
        '@radix-ui/react-use-size',
        '@radix-ui/react-visually-hidden',
        '@radix-ui/rect',
        '@rc-component/async-validator',
        '@rc-component/cascader',
        '@rc-component/checkbox',
        '@rc-component/collapse',
        '@rc-component/color-picker',
        '@rc-component/context',
        '@rc-component/dialog',
        '@rc-component/drawer',
        '@rc-component/dropdown',
        '@rc-component/form',
        '@rc-component/image',
        '@rc-component/input',
        '@rc-component/input-number',
        '@rc-component/mentions',
        '@rc-component/menu',
        '@rc-component/mini-decimal',
        '@rc-component/motion',
        '@rc-component/mutate-observer',
        '@rc-component/notification',
        '@rc-component/overflow',
        '@rc-component/pagination',
        '@rc-component/picker',
        '@rc-component/portal',
        '@rc-component/progress',
        '@rc-component/qrcode',
        '@rc-component/rate',
        '@rc-component/resize-observer',
        '@rc-component/segmented',
        '@rc-component/select',
        '@rc-component/slider',
        '@rc-component/steps',
        '@rc-component/switch',
        '@rc-component/table',
        '@rc-component/tabs',
        '@rc-component/tooltip',
        '@rc-component/tour',
        '@rc-component/tree',
        '@rc-component/tree-select',
        '@rc-component/trigger',
        '@rc-component/upload',
        '@rc-component/util',
        '@rc-component/virtual-list',
        '@use-gesture/core',
        '@use-gesture/react',
        'acorn',
        'agent-base',
        'ahooks',
        'aria-hidden',
        'asynckit',
        'attr-accept',
        'babel-plugin-macros',
        'call-bind-apply-helpers',
        'class-variance-authority',
        'classnames',
        'clsx',
        'colord',
        'combined-stream',
        'commander',
        'convert-source-map',
        'cookie',
        'cose-base',
        'cosmiconfig',
        'd3-sankey',
        'dayjs',
        // dayjs 插件子路径：@rc-component/picker（antd DatePicker 依赖）以
        // `import x from 'dayjs/plugin/xxx'` 引入 6 个插件。noDiscovery: true 下
        // 若 include 只列主包，插件子路径不会被预构建，浏览器直接以 ESM 加载
        // UMD 源文件报 "does not provide an export named 'default'"，React 挂载
        // 中断导致页面白屏。补全子路径由 rolldown 预构建（CJS interop 正确）。
        'dayjs/plugin/weekday',
        'dayjs/plugin/localeData',
        'dayjs/plugin/weekOfYear',
        'dayjs/plugin/weekYear',
        'dayjs/plugin/advancedFormat',
        'dayjs/plugin/customParseFormat',
        'debug',
        'delayed-stream',
        'dequal',
        'detect-node-es',
        'es-define-property',
        'es-errors',
        'es-object-atoms',
        'es-set-tostringtag',
        'es-toolkit',
        'extend',
        'extend-shallow',
        'fast-deep-equal',
        'file-selector',
        'find-root',
        'follow-redirects',
        'for-in',
        'form-data',
        'format',
        'framer-motion',
        'function-bind',
        'get-intrinsic',
        'get-nonce',
        'get-proto',
        'get-value',
        'gopd',
        'has-symbols',
        'has-tostringtag',
        'hasown',
        'highlight.js',
        'highlightjs-vue',
        'hoist-non-react-statics',
        'https-proxy-agent',
        'iconv-lite',
        'immer',
        'intersection-observer',
        'is-core-module',
        'is-extendable',
        'is-mobile',
        'is-plain-object',
        'isobject',
        'js-cookie',
        'jsesc',
        'json-parse-even-better-errors',
        'json2mq',
        'layout-base',
        'leva',
        'lines-and-columns',
        'lodash',
        'loose-envify',
        'lowlight/lib/core',
        'lru_map',
        'lucide-react',
        'merge-value',
        'mixin-deep',
        'motion',
        'motion-dom',
        'motion-utils',
        'ms',
        'numeral',
        'path-data-parser',
        'path-parse',
        'picocolors',
        'points-on-curve',
        'points-on-path',
        'polished',
        'prismjs',
        'prop-types',
        'rc-collapse',
        'rc-dialog',
        'rc-footer',
        'rc-image',
        'rc-input',
        'rc-input-number',
        'rc-menu',
        'rc-motion',
        'rc-overflow',
        'rc-resize-observer',
        'rc-util',
        're-resizable',
        'react',
        'react-avatar-editor',
        'react-colorful',
        'react-dom',
        'react-dom/client',
        'react-draggable',
        'react-dropzone',
        'react-fast-compare',
        'react-is',
        'react-remove-scroll',
        'react-remove-scroll-bar',
        'react-rnd',
        'react-router',
        'react-router-dom',
        'react-style-singleton',
        'react-syntax-highlighter',
        'react-zoom-pan-pinch',
        'reselect',
        'resize-observer-polyfill',
        'resolve',
        'rw',
        'safer-buffer',
        'screenfull',
        'semver-compare',
        'set-cookie-parser',
        'set-value',
        'sonner',
        'source-map',
        'split-string',
        'string-convert',
        'style-to-js',
        'supports-preserve-symlinks-flag',
        'swr',
        'tabbable',
        'tailwind-merge',
        'throttle-debounce',
        'tslib',
        'use-callback-ref',
        'use-merge-value',
        'use-sidecar',
        'use-sync-external-store',
        // use-sync-external-store 的 shim 子路径为 CJS（module.exports = require(...)），
        // zustand（esm/index.mjs → shim、esm/traditional.mjs → shim/with-selector.js）
        // 与 @base-ui/utils（store/useStore.mjs → shim/with-selector，注意无 .js 后缀）
        // 以 ESM import 引入。noDiscovery: true 下若只列主包，浏览器直接以 ESM 加载
        // CJS 源文件报 "does not provide an export named 'useSyncExternalStore'"，
        // React 挂载中断白屏。补全 shim 子路径（含无 .js 与带 .js 两种写法）由
        // rolldown 预构建，保证 vite 对两种 import 写法都能重写到预构建产物。
        'use-sync-external-store/shim',
        'use-sync-external-store/shim/with-selector',
        'use-sync-external-store/shim/with-selector.js',
        'v8n',
        'zustand',
        'zustand/middleware',
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
