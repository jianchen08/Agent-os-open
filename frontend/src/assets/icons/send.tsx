import React from "react";

const SendIcon: React.FC<React.SVGProps<SVGSVGElement>> = ({
  className = "",
  ...props
}) => {
  return (
    <svg
      viewBox="0 0 20 20"
      className={`send-icon ${className}`}
      fill="currentColor"
      {...props}
    >
      <path fill="#FFF" transform="matrix(1 0 0 1 2.49996 2.5)" d="M1.7094 7.5L-0.7115 0.2372Q-0.8195 -0.0582 -0.6708 -0.3354Q-0.5383 -0.6207 -0.2372 -0.7115Q-0.0996 -0.7579 0.0452 -0.7486Q0.1901 -0.7404 0.3211 -0.6778L16.1544 6.8222Q16.4424 6.9486 16.5396 7.2477Q16.6539 7.5408 16.5111 7.8211Q16.4558 7.9387 16.3637 8.0303Q16.272 8.1225 16.1544 8.1778L0.3211 15.6778Q0.0408 15.8206 -0.2523 15.7063Q-0.5514 15.6091 -0.6778 15.3211Q-0.7404 15.1901 -0.7486 15.0452Q-0.7579 14.9004 -0.7115 14.7628L1.7094 7.5ZM3.25 7.5Q3.2503 7.3783 3.2115 7.2628L1.2673 1.4302L14.0814 7.5L1.2673 13.5698L3.2115 7.7372Q3.2503 7.6217 3.25 7.5Z" fillRule="evenodd"/><rect fill="#FFF" transform="matrix(1 0 0 1 4.99996 10)" y="-0.75" width="13.3333" height="1.5"/>
    </svg>
  );
};

export default SendIcon;