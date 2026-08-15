import React from "react";

const GlobeIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`globe-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <circle fill="none" stroke="#94A3B8" strokeWidth="1.8" transform="matrix(1 0 0 1 2.5 2.5)" cx="7.5" cy="7.5" r="7.5"/><path transform="matrix(1 0 0 1 2.5 2.5)" d="M11.3713 8.4Q11.0939 12.6789 8.1364 15.6364L7.5 16.2728L6.8636 15.6364Q3.9061 12.6789 3.6287 8.4L0 8.4L0 6.6L3.6287 6.6Q3.9061 2.3211 6.8636 -0.6364L7.5 -1.2728L8.1364 -0.6364Q11.0939 2.3211 11.3713 6.6L15 6.6L15 8.4L11.3713 8.4ZM9.5662 8.4Q9.3336 11.4388 7.5 13.6638Q5.6664 11.4388 5.4338 8.4L9.5662 8.4ZM9.5662 6.6Q9.3336 3.5612 7.5 1.3362Q5.6664 3.5612 5.4338 6.6L9.5662 6.6Z" fillRule="evenodd"/>
    </svg>
  );
};

export default GlobeIcon;
