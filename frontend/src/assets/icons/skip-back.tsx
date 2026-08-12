import React from "react";

const SkipBackIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`skip-back-icon ${className}`}
      fill="currentColor"
      stroke="none"
      {...props}
    >
      <polygon points="19 20 9 12 19 4 19 20" />
      <line x1="5" y1="19" x2="5" y2="5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
};

export default SkipBackIcon;
