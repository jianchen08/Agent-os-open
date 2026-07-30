import React from "react";

/**
 * 隐藏/显示工作区 — 方框带右侧垂直分隔线(右侧面板)
 */
const PanelRightIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`panel-right-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {/* 外框 */}
      <rect x="3.5" y="4.5" width="13" height="11" rx="1.2" />
      {/* 右侧垂直分隔线(标识右侧面板为工作区) */}
      <path d="M13 4.5 V15.5" />
    </svg>
  );
};

export default PanelRightIcon;
