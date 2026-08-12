import React from "react";

const ChevronUpIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 24 24"
      className={`chevron-up-icon ${className}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <polyline points="18 15 12 9 6 15" />
    </svg>
  );
};

export default ChevronUpIcon;
