import React from "react";

const MoreIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`more-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <circle transform="matrix(1 0 0 1 2.83329 8.66668)" cx="1.3333" cy="1.3333" r="1.3333"/><circle transform="matrix(1 0 0 1 8.66667 8.66668)" cx="1.3333" cy="1.3333" r="1.3333"/><circle transform="matrix(1 0 0 1 14.5 8.66668)" cx="1.3333" cy="1.3333" r="1.3333"/>
    </svg>
  );
};

export default MoreIcon;
