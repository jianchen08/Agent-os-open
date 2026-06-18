FROM node:20-alpine
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/scripts/ ./scripts/
RUN node  $(which npm) ci --registry=https://registry.npmmirror.com --no-audit --no-fund --ignore-scripts 2>&1 | tail -2; exit ${PIPESTATUS[0]}
