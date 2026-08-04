import React from "react";

const StarIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`star-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path
        d="M10 1.5L12.06 7.17L18.08 7.37L13.33 11.08L15 16.88L10 13.5L5 16.88L6.67 11.08L1.92 7.37L7.94 7.17Z"
        fillRule="evenodd"
      />
    </svg>
  );
};

export default StarIcon;
