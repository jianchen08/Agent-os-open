import React from "react";

const EditIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`edit-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path transform="matrix(1 0 0 1 4 3.5)" d="M13.4 3Q13.4109 2.6227 13.1364 2.3636L10.1364 -0.6364Q9.8773 -0.9109 9.5 -0.9Q9.1227 -0.9109 8.8636 -0.6364L-0.6364 8.8636Q-0.7636 8.9898 -0.8315 9.1556Q-0.9007 9.3208 -0.9 9.5L-0.9 12.5Q-0.9109 12.8773 -0.6364 13.1364Q-0.3773 13.4109 0 13.4L3 13.4Q3.1792 13.4007 3.3444 13.3315Q3.5102 13.2636 3.6364 13.1364L13.1364 3.6364Q13.4109 3.3773 13.4 3ZM11.2272 3L9.5 1.2728L0.9 9.8728L0.9 11.6L2.6272 11.6L11.2272 3Z" fillRule="evenodd"/><path transform="matrix(1 0 0 1 11.5 5.5)" d="M3.6364 2.3636L0.6364 -0.6364L-0.6364 0.6364L2.3636 3.6364L3.6364 2.3636Z" fillRule="evenodd"/>
    </svg>
  );
};

export default EditIcon;