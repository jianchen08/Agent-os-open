import React from "react";

const BoxIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`box-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 3 2.5)" d="M7 -0.9Q6.7872 -0.9013 6.5975 -0.805L-0.4025 2.695Q-0.633 2.8073 -0.7656 3.0269Q-0.9027 3.2436 -0.9 3.5L-0.9 11.5Q-0.9027 11.7564 -0.7656 11.9731Q-0.633 12.1927 -0.4025 12.305L6.5975 15.805Q6.7872 15.9013 7 15.9Q7.2128 15.9013 7.4025 15.805L14.4025 12.305Q14.633 12.1927 14.7656 11.9731Q14.9027 11.7564 14.9 11.5L14.9 3.5Q14.9027 3.2436 14.7656 3.0269Q14.633 2.8073 14.4025 2.695L7.4025 -0.805Q7.2128 -0.9013 7 -0.9ZM7 1.0062L0.9 4.0562L0.9 10.9438L7 13.9938L13.1 10.9438L13.1 4.0562L7 1.0062Z" fillRule="evenodd"/><path transform="matrix(1 0 0 1 3 6)" d="M0.4025 -0.805L7 2.4938L13.5975 -0.805L14.4025 0.805L7.9 4.0562L7.9 11.5L6.1 11.5L6.1 4.0562L-0.4025 0.805L0.4025 -0.805Z" fillRule="evenodd"/>
    </svg>
  );
};

export default BoxIcon;
