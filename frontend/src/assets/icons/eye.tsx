import React from "react";

const EyeIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`eye-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 2 4.5)" d="M13.0772 0.7367Q10.7298 -0.9 8 -0.9Q5.2702 -0.9 2.9228 0.7367Q0.6729 2.3054 -0.7954 5.0789Q-0.9016 5.2761 -0.9 5.5Q-0.9016 5.7239 -0.7954 5.9211Q0.6729 8.6946 2.9228 10.2633Q5.2702 11.9 8 11.9Q10.7298 11.9 13.0772 10.2633Q15.3271 8.6946 16.7954 5.9211Q16.9016 5.7239 16.9 5.5Q16.9016 5.2761 16.7954 5.0789Q15.3271 2.3054 13.0772 0.7367ZM3.9522 2.2133Q5.8357 0.9 8 0.9Q10.1643 0.9 12.0478 2.2133Q13.7699 3.414 14.972 5.5Q13.7699 7.586 12.0478 8.7867Q10.1643 10.1 8 10.1Q5.8357 10.1 3.9522 8.7867Q2.2301 7.586 1.028 5.5Q2.2301 3.414 3.9522 2.2133Z" fillRule="evenodd"/><circle fill="none" stroke="#94A3B8" strokeWidth="1.8" transform="matrix(1 0 0 1 7.5 7.5)" cx="2.5" cy="2.5" r="2.5"/>
    </svg>
  );
};

export default EyeIcon;
