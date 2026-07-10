/**
 * 高对比度主题
 *
 * 专为视觉障碍用户设计的高对比度主题
 * 符合 WCAG 2.1 AAA 标准，提供最佳的可访问性
 */

import type { ThemeConfig } from '@/types/theme'

export const highContrastTheme: ThemeConfig = {
  id: 'high-contrast',
  name: '高对比度',
  description: '专为视觉障碍用户设计的高对比度主题，符合WCAG 2.1 AAA标准',
  category: 'special',

  colors: {
    primary: '#ffffff',
    secondary: '#ffff00',
    accent: '#00ff00',

    background: {
      main: '#000000',
      card: '#1a1a1a',
      sidebar: '#0d0d0d',
      input: '#333333',
      elevated: '#262626',
    },

    text: {
      primary: '#ffffff',
      secondary: '#ffff00',
      muted: '#cccccc',
      disabled: '#808080',
    },

    border: {
      default: '#ffffff',
      hover: '#ffff00',
      active: '#00ff00',
    },

    status: {
      success: '#00ff00',
      warning: '#ffff00',
      error: '#ff0000',
      info: '#00ffff',
      running: '#00ffff',
      pending: '#ffffff',
    },

    bubble: {
      user_bg: '#ffffff',
      user_text: '#000000',
      user_radius: '0.25rem',
      user_shadow: 'none',
      user_border: '2px solid #ffffff',
      ai_bg: '#333333',
      ai_text: '#ffffff',
      ai_radius: '0.25rem',
      ai_shadow: 'none',
      ai_border: '2px solid #ffffff',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0',
      md: '0',
      lg: '0',
      xl: '0',
      full: '9999px',
      defaultRadius: 'none',
    },

    fonts: {
      ui: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '15px',
      sm: '16px',
      md: '17px',
      lg: '18px',
      xl: '19px',
      defaultFontSize: 'md',
    },

    shadows: {
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: { sm: 'none', md: 'none', lg: 'none' },
      normal: { sm: 'none', md: 'none', lg: 'none' },
      strong: { sm: 'none', md: 'none', lg: 'none' },
      defaultShadow: 'none',
    },

    glow: {
      running: '0 0 0 2px #00ffff',
      waiting: '0 0 0 2px #ffff00',
      success: '0 0 0 2px #00ff00',
      error: '0 0 0 2px #ff0000',
      defaultGlowIntensity: 0,
    },

    button: {
      style: 'square',
      shadow: false,
      borderWidth: '2px',
      hoverEffect: 'darken',
      texture: 'none',
      textureOpacity: 0,
      variants: {
        primary: {
          bg: '#ffffff',
          text: '#000000',
          border: '2px solid #ffffff',
          hoverBg: '#cccccc',
        },
        secondary: {
          bg: '#000000',
          text: '#ffffff',
          border: '2px solid #ffffff',
          hoverBg: '#333333',
        },
        ghost: {
          bg: 'transparent',
          text: '#ffffff',
          border: '2px solid #ffffff',
          hoverBg: '#333333',
        },
        destructive: {
          bg: '#cc0000',
          text: '#ffffff',
          border: '2px solid #ff0000',
          hoverBg: '#990000',
        },
      },
    },

    input: {
      style: 'outlined',
      focusBorder: '#ffff00',
      focusGlow: 'none',
    },

    card: {
      style: 'solid',
      blur: '0',
      border: '2px solid #ffffff',
    },

    badge: {
      borderRadius: '0',
      variants: {
        default: {
          bg: '#ffffff',
          text: '#000000',
          border: '2px solid #ffffff',
        },
        secondary: {
          bg: '#000000',
          text: '#ffffff',
          border: '2px solid #ffffff',
        },
        success: {
          bg: '#00ff00',
          text: '#000000',
          border: '2px solid #00ff00',
        },
        warning: {
          bg: '#ffff00',
          text: '#000000',
          border: '2px solid #ffff00',
        },
        error: {
          bg: '#ff0000',
          text: '#ffffff',
          border: '2px solid #ff0000',
        },
        info: {
          bg: '#00ffff',
          text: '#000000',
          border: '2px solid #00ffff',
        },
      },
    },

    dialog: {
      borderRadius: '0',
      overlayBg: '#000000',
      overlayOpacity: 0.9,
      shadow: 'none',
      border: '2px solid #ffffff',
    },

    tabs: {
      borderRadius: '0',
      listBg: '#000000',
      activeBg: '#ffffff',
      activeText: '#000000',
      inactiveText: '#ffffff',
    },

    toast: {
      borderRadius: '0',
      shadow: 'none',
      variants: {
        default: {
          bg: '#000000',
          text: '#ffffff',
          border: '2px solid #ffffff',
        },
        success: {
          bg: '#00ff00',
          text: '#000000',
          border: '2px solid #00ff00',
        },
        error: {
          bg: '#ff0000',
          text: '#ffffff',
          border: '2px solid #ff0000',
        },
        warning: {
          bg: '#ffff00',
          text: '#000000',
          border: '2px solid #ffff00',
        },
        info: {
          bg: '#00ffff',
          text: '#000000',
          border: '2px solid #00ffff',
        },
      },
    },

    progress: {
      borderRadius: '0',
      trackBg: '#333333',
      variants: {
        default: '#ffffff',
        success: '#00ff00',
        warning: '#ffff00',
        error: '#ff0000',
      },
    },

    dropdownMenu: {
      borderRadius: '0',
      shadow: 'none',
      border: '2px solid #ffffff',
      itemHoverBg: '#333333',
      itemHoverText: '#ffff00',
    },
  },

  effects: {
    glassmorphism: false,
    animations: false,
    transitionDuration: 0,
    transitionEasing: 'linear',
  },

  backgrounds: {
    main: {
      type: 'solid',
      value: '#000000',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: '#000000',
      overlayOpacity: 1,
    },
    texture: {
      type: 'none',
      color: 'transparent',
      size: '0',
      opacity: 0,
    },
    sidebar: {
      type: 'solid',
      value: '#0d0d0d',
      texture: { type: 'none' },
    },
    chat: {
      type: 'solid',
      value: '#000000',
    },
  },
}
