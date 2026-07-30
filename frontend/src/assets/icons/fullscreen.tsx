import React from "react";

/**
 * 全屏(进入) — 四个角向中心外扩的直角箭头
 */
const FullscreenIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`fullscreen-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M3.5 7.5 V4.5 a1 1 0 0 1 1-1 H7.5" />
      <path d="M12.5 3.5 H15.5 a1 1 0 0 1 1 1 V7.5" />
      <path d="M16.5 12.5 V15.5 a1 1 0 0 1 -1 1 H12.5" />
      <path d="M7.5 16.5 H4.5 a1 1 0 0 1 -1 -1 V12.5" />
    </svg>
  );
};

export default FullscreenIcon;
