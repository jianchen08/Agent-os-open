import React from "react";

const ChevronDownIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`chevron-down-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 5 7.5)" d="M5 3.9393L0.5303 -0.5303Q0.3144 -0.759 0 -0.75Q-0.3144 -0.759 -0.5303 -0.5303Q-0.759 -0.3144 -0.75 0Q-0.759 0.3144 -0.5303 0.5303L4.4697 5.5303Q4.6856 5.759 5 5.75Q5.3144 5.759 5.5303 5.5303L10.5303 0.5303Q10.759 0.3144 10.75 0Q10.759 -0.3144 10.5303 -0.5303Q10.3144 -0.759 10 -0.75Q9.6856 -0.759 9.4697 -0.5303L5 3.9393Z" fillRule="evenodd"/>
    </svg>
  );
};

export default ChevronDownIcon;