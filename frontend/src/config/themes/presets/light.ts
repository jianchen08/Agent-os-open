/**
 * 浅色主题配置
 *
 * 默认浅色主题，适合日间使用
 */

import type { ThemeConfig } from '@/types/theme'

export const lightTheme: ThemeConfig = {
  id: 'light',
  name: '浅色主题',
  description: '默认浅色主题，适合日间使用',
  category: 'light',

  colors: {
    primary: '#2563eb',
    secondary: '#4f46e5',
    accent: '#7c3aed',

    background: {
      main: '#ffffff',
      card: '#ffffff',
      sidebar: '#f8fafc',
      input: '#f1f5f9',
      elevated: '#ffffff',
    },

    text: {
      primary: '#0f172a',
      secondary: '#475569',
      muted: '#64748b',
      disabled: '#94a3b8',
    },

    border: {
      default: 'rgba(0, 0, 0, 0.08)',
      hover: 'rgba(0, 0, 0, 0.15)',
      active: 'rgba(37, 99, 235, 0.4)',
    },

    status: {
      success: '#059669',
      warning: '#d97706',
      error: '#dc2626',
      info: '#2563eb',
      running: '#0891b2',
      pending: '#64748b',
    },

    bubble: {
      user_bg: '#2563eb',
      user_text: '#ffffff',
      user_radius: '1.5rem 1.5rem 1.5rem 0.25rem',
      user_shadow: '0 4px 12px rgba(37, 99, 235, 0.25)',
      ai_bg: '#e2e8f0',
      ai_text: '#0f172a',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.08)',
      ai_border: '1px solid rgba(0, 0, 0, 0.06)',
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
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.03)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.05)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.05)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.1)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.1)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.1)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.15)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.2)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 15px rgba(8, 145, 178, 0.2)',
      waiting: '0 0 15px rgba(217, 119, 6, 0.2)',
      success: '0 0 15px rgba(5, 150, 105, 0.2)',
      error: '0 0 15px rgba(220, 38, 38, 0.2)',
      defaultGlowIntensity: 20,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'darken',
      texture: 'none',
      textureOpacity: 0,
      variants: {
        primary: {
          bg: '#2563eb',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#1d4ed8',
        },
        secondary: {
          bg: '#f1f5f9',
          text: '#0f172a',
          border: '#e2e8f0',
          hoverBg: '#e2e8f0',
        },
        ghost: {
          bg: 'transparent',
          text: '#475569',
          border: 'transparent',
          hoverBg: 'rgba(0,0,0,0.05)',
        },
        destructive: {
          bg: '#dc2626',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#b91c1c',
        },
      },
    },

    input: {
      style: 'outlined',
      focusBorder: '#2563eb',
      focusGlow: '0 0 0 3px rgba(37, 99, 235, 0.1)',
    },

    card: {
      style: 'elevated',
      blur: '0',
      border: '1px solid rgba(0, 0, 0, 0.08)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#2563eb',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: '#f1f5f9',
          text: '#0f172a',
          border: 'transparent',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.5,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
      border: '1px solid rgba(0, 0, 0, 0.1)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: '#f1f5f9',
      activeBg: '#ffffff',
      activeText: '#0f172a',
      inactiveText: '#64748b',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      variants: {
        default: {
          bg: '#ffffff',
          text: '#0f172a',
          border: 'rgba(0, 0, 0, 0.1)',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(37, 99, 235, 0.2)',
      variants: {
        default: '#2563eb',
        success: '#059669',
        warning: '#d97706',
        error: '#dc2626',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      border: '1px solid rgba(0, 0, 0, 0.1)',
      itemHoverBg: '#f1f5f9',
      itemHoverText: '#0f172a',
    },
  },

  effects: {
    glassmorphism: false,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.4, 0, 0.2, 1)',
  },

  backgrounds: {
    main: { type: 'solid', value: '#f8fafc' },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(255, 255, 255, 0.9)',
      overlayOpacity: 0.9,
    },
    texture: {
      type: 'none',
      color: 'rgba(0, 0, 0, 0.02)',
      size: '24px',
      opacity: 0.05,
    },
    sidebar: { type: 'solid', value: '#ffffff', texture: { type: 'none' } },
    chat: { type: 'solid', value: '#f8fafc' },
  },
}
