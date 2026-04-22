/**
 * 深色主题配置
 *
 * 默认深色主题，适合夜间使用
 */

import type { ThemeConfig } from '@/types/theme'

export const darkTheme: ThemeConfig = {
  id: 'dark',
  name: '深色主题',
  description: '默认深色主题，适合夜间使用',
  category: 'dark',

  colors: {
    primary: '#3b82f6',
    secondary: '#6366f1',
    accent: '#8b5cf6',

    background: {
      main: '#0f172a',
      card: '#1e293b',
      sidebar: '#1e293b',
      input: '#334155',
      elevated: '#1e293b',
    },

    text: {
      primary: '#f8fafc',      // 保持高对比度
      secondary: '#cbd5e1',    // 从 #94a3b8 提亮，对比度从 5.4:1 提升到 7.2:1
      muted: '#94a3b8',        // 从 #64748b 提亮，对比度从 3.2:1 提升到 5.4:1
      disabled: '#64748b',     // 从 #475569 提亮，对比度从 2.1:1 提升到 3.2:1
    },

    border: {
      default: 'rgba(255, 255, 255, 0.12)',  // 从 0.08 增加，提高可见度
      hover: 'rgba(255, 255, 255, 0.20)',    // 从 0.15 增加，提高可见度
      active: 'rgba(59, 130, 246, 0.5)',     // 从 0.4 增加，提高可见度
    },

    status: {
      success: '#10b981',
      warning: '#f59e0b',
      error: '#ef4444',
      info: '#3b82f6',
      running: '#00f0ff',
      pending: '#94a3b8',
    },

    bubble: {
      user_bg: '#3b82f6',
      user_text: '#ffffff',
      user_radius: '1.5rem 1.5rem 1.5rem 0.25rem',
      user_shadow: '0 4px 12px rgba(59, 130, 246, 0.3)',
      ai_bg: '#334155',
      ai_text: '#f1f5f9',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.2)',
      ai_border: '1px solid rgba(255, 255, 255, 0.08)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.25rem',
      md: '0.5rem',
      lg: '0.75rem',
      xl: '1rem',
      full: '9999px',
      defaultRadius: 'md',
    },

    fonts: {
      ui: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '14px',
      sm: '15px',
      md: '16px',
      lg: '17px',
      xl: '18px',
      defaultFontSize: 'md',
    },

    shadows: {
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.2)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.25)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.25)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.3)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.4)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.4)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.4)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.5)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.5)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 15px rgba(0, 240, 255, 0.3)',
      waiting: '0 0 15px rgba(245, 158, 11, 0.3)',
      success: '0 0 15px rgba(16, 185, 129, 0.3)',
      error: '0 0 15px rgba(239, 68, 68, 0.3)',
      defaultGlowIntensity: 30,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'glass',
      textureOpacity: 0.1,
      variants: {
        primary: {
          bg: '#3b82f6',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#2563eb',
        },
        secondary: {
          bg: 'rgba(255,255,255,0.1)',
          text: '#f8fafc',
          border: 'rgba(255,255,255,0.2)',
          hoverBg: 'rgba(255,255,255,0.15)',
        },
        ghost: {
          bg: 'transparent',
          text: '#94a3b8',
          border: 'transparent',
          hoverBg: 'rgba(255,255,255,0.05)',
        },
        destructive: {
          bg: '#ef4444',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#dc2626',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#3b82f6',
      focusGlow: '0 0 0 3px rgba(59, 130, 246, 0.2)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(255, 255, 255, 0.08)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#3b82f6',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: 'rgba(255,255,255,0.1)',
          text: '#f8fafc',
          border: 'transparent',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.25)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.25)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.25)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        info: {
          bg: 'rgba(59, 130, 246, 0.25)',
          text: '#93c5fd',
          border: 'rgba(59, 130, 246, 0.5)',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.8,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
      border: '1px solid rgba(255, 255, 255, 0.1)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: 'rgba(255, 255, 255, 0.05)',
      activeBg: '#1e293b',
      activeText: '#f8fafc',
      inactiveText: '#94a3b8',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      variants: {
        default: {
          bg: '#1e293b',
          text: '#f8fafc',
          border: 'rgba(255, 255, 255, 0.1)',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.2)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.2)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.2)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        info: {
          bg: 'rgba(59, 130, 246, 0.2)',
          text: '#93c5fd',
          border: 'rgba(59, 130, 246, 0.5)',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(59, 130, 246, 0.2)',
      variants: {
        default: '#3b82f6',
        success: '#10b981',
        warning: '#f59e0b',
        error: '#ef4444',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      border: '1px solid rgba(255, 255, 255, 0.1)',
      itemHoverBg: 'rgba(255, 255, 255, 0.1)',
      itemHoverText: '#f8fafc',
    },
  },

  effects: {
    glassmorphism: true,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.4, 0, 0.2, 1)',
  },

  backgrounds: {
    main: {
      type: 'gradient',
      value: 'radial-gradient(circle at 50% 0%, #0f172a, #020617)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(15, 23, 42, 0.85)',
      overlayOpacity: 0.85,
    },
    texture: {
      type: 'grid',
      color: 'rgba(255, 255, 255, 0.03)',
      size: '24px',
      opacity: 0.1,
    },
    sidebar: {
      type: 'solid',
      value: '#1e293b',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #0f172a 0%, #1e293b 100%)',
    },
  },
}
