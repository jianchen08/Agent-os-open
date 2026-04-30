/**
 * 深空指挥台主题
 *
 * 专业级深色主题，模拟太空指挥中心的科技感界面
 * 带有强烈的发光效果和科技感视觉
 */

import type { ThemeConfig } from '@/types/theme'

export const deepSpaceTheme: ThemeConfig = {
  id: 'deep-space',
  name: '深空指挥台',
  description: '专业级深色主题，模拟太空指挥中心的科技感界面',
  category: 'dark',

  colors: {
    primary: '#00f0ff',
    secondary: '#7c3aed',
    accent: '#f59e0b',

    background: {
      main: 'radial-gradient(circle at 20% 80%, #1e1b4b 0%, #0f172a 40%, #020617 100%)',
      card: 'rgba(15, 23, 42, 0.7)',
      sidebar: 'rgba(30, 27, 75, 0.8)',
      input: 'rgba(15, 23, 42, 0.6)',
      elevated: 'rgba(30, 41, 59, 0.8)',
    },

    text: {
      primary: '#f8fafc', // 保持高对比度
      secondary: '#22d3ee', // 从 #00f0ff 调暗，提高可读性
      muted: '#a5f3fc', // 从 #94a3b8 提亮，对比度 >= 4.5:1
      disabled: '#64748b', // 从 #475569 提亮，对比度 >= 3:1
    },

    border: {
      default: 'rgba(0, 240, 255, 0.25)', // 从 0.2 增加
      hover: 'rgba(0, 240, 255, 0.45)', // 从 0.4 增加
      active: 'rgba(0, 240, 255, 0.65)', // 从 0.6 增加
    },

    status: {
      success: '#10b981',
      warning: '#f59e0b',
      error: '#ef4444',
      info: '#00f0ff',
      running: '#00f0ff',
      pending: '#7c3aed',
    },

    bubble: {
      user_bg: 'linear-gradient(135deg, #00f0ff 0%, #0891b2 100%)',
      user_text: '#020617',
      user_radius: '0.5rem 0.5rem 0.5rem 0.125rem',
      user_shadow: '0 0 15px rgba(0, 240, 255, 0.4), 0 0 30px rgba(0, 240, 255, 0.15)',
      user_border: '1px solid rgba(0, 240, 255, 0.4)',
      ai_bg: '#1e293b',
      ai_text: '#f8fafc',
      ai_radius: '0.375rem 0.375rem 0.375rem 0.125rem',
      ai_shadow: '0 0 10px rgba(0, 240, 255, 0.1)',
      ai_border: '1px solid rgba(0, 240, 255, 0.2)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.125rem',
      md: '0.25rem',
      lg: '0.375rem',
      xl: '0.5rem',
      full: '9999px',
      defaultRadius: 'sm',
    },

    fonts: {
      ui: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '13px',
      sm: '14px',
      md: '15px',
      lg: '16px',
      xl: '17px',
      defaultFontSize: 'md',
    },

    shadows: {
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.3)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.35)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.35)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.4)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.5)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.5)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.5)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.6)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.6)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 25px rgba(0, 240, 255, 0.6)',
      waiting: '0 0 25px rgba(245, 158, 11, 0.6)',
      success: '0 0 25px rgba(16, 185, 129, 0.6)',
      error: '0 0 25px rgba(239, 68, 68, 0.6)',
      defaultGlowIntensity: 60,
    },

    button: {
      style: 'square',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'glow',
      texture: 'noise',
      textureOpacity: 0.3,
      variants: {
        primary: {
          bg: 'linear-gradient(135deg, #00f0ff 0%, #0891b2 100%)',
          text: '#020617',
          border: '1px solid #00f0ff',
          hoverBg: 'linear-gradient(135deg, #22d3ee 0%, #00f0ff 100%)',
        },
        secondary: {
          bg: 'rgba(0, 240, 255, 0.1)',
          text: '#00f0ff',
          border: '1px solid rgba(0, 240, 255, 0.3)',
          hoverBg: 'rgba(0, 240, 255, 0.2)',
        },
        ghost: {
          bg: 'transparent',
          text: '#94a3b8',
          border: 'transparent',
          hoverBg: 'rgba(0, 240, 255, 0.1)',
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
      focusBorder: '#00f0ff',
      focusGlow: '0 0 0 3px rgba(0, 240, 255, 0.3)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(0, 240, 255, 0.2)',
    },

    badge: {
      borderRadius: '0.25rem',
      variants: {
        default: {
          bg: 'linear-gradient(135deg, #00f0ff 0%, #0891b2 100%)',
          text: '#020617',
          border: '#00f0ff',
        },
        secondary: {
          bg: 'rgba(0, 240, 255, 0.15)',
          text: '#a5f3fc',
          border: 'rgba(0, 240, 255, 0.4)',
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
          bg: 'rgba(0, 240, 255, 0.25)',
          text: '#a5f3fc',
          border: 'rgba(0, 240, 255, 0.5)',
        },
      },
    },

    dialog: {
      borderRadius: '0.5rem',
      overlayBg: '#000000',
      overlayOpacity: 0.85,
      shadow: '0 0 30px rgba(0, 240, 255, 0.3)',
      border: '1px solid rgba(0, 240, 255, 0.3)',
    },

    tabs: {
      borderRadius: '0.25rem',
      listBg: 'rgba(0, 240, 255, 0.1)',
      activeBg: 'rgba(0, 240, 255, 0.2)',
      activeText: '#a5f3fc',
      inactiveText: '#94a3b8',
    },

    toast: {
      borderRadius: '0.375rem',
      shadow: '0 0 20px rgba(0, 240, 255, 0.3)',
      variants: {
        default: {
          bg: 'rgba(15, 23, 42, 0.95)',
          text: '#a5f3fc',
          border: 'rgba(0, 240, 255, 0.3)',
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
          bg: 'rgba(0, 240, 255, 0.2)',
          text: '#a5f3fc',
          border: 'rgba(0, 240, 255, 0.5)',
        },
      },
    },

    progress: {
      borderRadius: '0.125rem',
      trackBg: 'rgba(0, 240, 255, 0.2)',
      variants: {
        default: 'linear-gradient(90deg, #00f0ff 0%, #0891b2 100%)',
        success: '#10b981',
        warning: '#f59e0b',
        error: '#ef4444',
      },
    },

    dropdownMenu: {
      borderRadius: '0.375rem',
      shadow: '0 0 20px rgba(0, 240, 255, 0.3)',
      border: '1px solid rgba(0, 240, 255, 0.3)',
      itemHoverBg: 'rgba(0, 240, 255, 0.15)',
      itemHoverText: '#a5f3fc',
    },
  },

  effects: {
    glassmorphism: false,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.4, 0, 0.2, 1)',
  },

  backgrounds: {
    main: {
      type: 'gradient',
      value: 'radial-gradient(circle at 20% 80%, #1e1b4b 0%, #0f172a 40%, #020617 100%)',
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
      color: 'rgba(0, 240, 255, 0.08)',
      size: '24px',
      opacity: 0.6,
    },
    sidebar: {
      type: 'solid',
      value: 'rgba(30, 27, 75, 0.8)',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'radial-gradient(circle at 20% 80%, #1e1b4b 0%, #0f172a 40%, #020617 100%)',
    },
  },
}
