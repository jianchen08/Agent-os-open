/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
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
        // 状态色
        status: {
          success: 'var(--status-success)',
          error: 'var(--status-error)',
          warning: 'var(--status-warning)',
          info: 'var(--status-info)',
          running: 'var(--status-running)',
          pending: 'var(--status-pending)',
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
        ui: ['Inter', 'sans-serif'],
        code: ['JetBrains Mono', 'monospace'],
      },
      boxShadow: {
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
          '50%': { borderColor: 'rgba(0, 240, 255, 0.5)' },
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
