/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontSize: {
        // 字号语义阶梯（统一审查 §4.1）：替代任意值 text-[10px]/[11px]/[12px]/[13px]
        caption: ['var(--font-size-caption)', { lineHeight: '1.2' }],
        label: ['var(--font-size-label)', { lineHeight: '1.3' }],
        body: ['var(--font-size-body)', { lineHeight: '1.4' }],
        title: ['var(--font-size-title)', { lineHeight: '1.4' }],
        'page-title': ['var(--font-size-page-title)', { lineHeight: '1.5' }],
      },
      width: {
        // 图标尺寸阶梯（统一审查 §4.1 / §3.2 C2）：替代 h-3/h-3.5/h-4/h-5/h-8 混用
        'icon-xs': 'var(--icon-size-xs)',
        'icon-sm': 'var(--icon-size-sm)',
        'icon-md': 'var(--icon-size-md)',
      },
      height: {
        'icon-xs': 'var(--icon-size-xs)',
        'icon-sm': 'var(--icon-size-sm)',
        'icon-md': 'var(--icon-size-md)',
      },
      borderRadius: {
        lg: "var(--radius-lg)",
        md: "var(--radius-md)",
        sm: "var(--radius-sm)",
        xl: "var(--radius-xl)",
        '2xl': "var(--radius-2xl)",
      },
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        chart: {
          "1": "hsl(var(--chart-1))",
          "2": "hsl(var(--chart-2))",
          "3": "hsl(var(--chart-3))",
          "4": "hsl(var(--chart-4))",
          "5": "hsl(var(--chart-5))",
        },
        // === Deep Space 主题色 ===
        // 状态色：rgb 三元组 + <alpha-value>，使 /10 /80 等透明度修饰真实生效
        // （裸 hex var 的 alpha 修饰会被编译成非法值被浏览器静默丢弃）
        status: {
          success: 'rgb(var(--status-success-rgb) / <alpha-value>)',
          'success-foreground': 'var(--status-success-foreground)',
          error: 'rgb(var(--status-error-rgb) / <alpha-value>)',
          'error-foreground': 'var(--status-error-foreground)',
          warning: 'rgb(var(--status-warning-rgb) / <alpha-value>)',
          'warning-foreground': 'var(--status-warning-foreground)',
          info: 'rgb(var(--status-info-rgb) / <alpha-value>)',
          'info-foreground': 'var(--status-info-foreground)',
          running: 'rgb(var(--status-running-rgb) / <alpha-value>)',
          'running-foreground': 'var(--status-running-foreground)',
          pending: 'rgb(var(--status-pending-rgb) / <alpha-value>)',
          'pending-foreground': 'var(--status-pending-foreground)',
        },
        // 背景色
        surface: {
          DEFAULT: 'var(--bg-panel)',
          elevated: 'var(--bg-elevated)',
          input: 'var(--bg-input)',
        },
        // 文字色
        text: {
          primary: 'var(--text-primary)',
          secondary: 'var(--text-secondary)',
          muted: 'var(--text-muted)',
          disabled: 'var(--text-disabled)',
        },
      },
      backgroundColor: {
        'deep-space': 'var(--bg-main)',
      },
      backgroundImage: {
        'grid-pattern': `linear-gradient(var(--border-default) 1px, transparent 1px),
                          linear-gradient(90deg, var(--border-default) 1px, transparent 1px)`,
      },
      fontFamily: {
        // 主题驱动：--font-ui/--font-code 由主题引擎按主题输出
        // （值本身即完整 fallback 栈），变量缺失时回退 design-tokens 静态定义
        ui: ['var(--font-ui, var(--font-family))'],
        code: ['var(--font-code, var(--font-family-mono))'],
      },
      boxShadow: {
        // 阴影全站主题化：跟随主题 shadows 配置（像素=零模糊硬阴影 / 软萌=大模糊弥散）
        // 引擎未运行时回退 design-tokens 静态默认
        sm: 'var(--shadow-normal-sm, var(--shadow-sm))',
        DEFAULT: 'var(--shadow-normal-md, var(--shadow-md))',
        md: 'var(--shadow-normal-md, var(--shadow-md))',
        lg: 'var(--shadow-normal-lg, var(--shadow-lg))',
        xl: 'var(--shadow-strong-lg, var(--shadow-xl))',
        '2xl': 'var(--shadow-strong-lg, var(--shadow-xl))',
        inner: 'inset 0 2px 4px 0 rgb(0 0 0 / 0.05)',
        none: 'none',
        'glow-running': 'var(--shadow-glow-running)',
        'glow-waiting': 'var(--shadow-glow-waiting)',
      },
      animation: {
        'border-flow': 'border-flow 2s linear infinite',
        'scale-pulse': 'scale-pulse 2s ease-in-out infinite',
        'fade-in': 'fade-in 0.3s ease-out',
        'slide-in': 'slide-in 0.2s ease-out',
      },
      keyframes: {
        'border-flow': {
          '0%, 100%': { borderColor: 'var(--accent-running)' },
          '50%': { borderColor: 'color-mix(in srgb, var(--accent-running, #00f0ff) 50%, transparent)' },
        },
        'scale-pulse': {
          '0%, 100%': { transform: 'scale(1)' },
          '50%': { transform: 'scale(1.05)' },
        },
        'fade-in': {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        'slide-in': {
          '0%': { transform: 'translateY(-10px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}
