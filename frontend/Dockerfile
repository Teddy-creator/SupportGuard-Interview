ARG NODE_BASE_IMAGE=node:22-alpine
ARG NGINX_BASE_IMAGE=nginxinc/nginx-unprivileged:1.27-alpine
ARG CODE_VERSION=development
FROM ${NODE_BASE_IMAGE} AS builder

WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

FROM ${NGINX_BASE_IMAGE}
ARG CODE_VERSION=development
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
LABEL org.opencontainers.image.revision="${CODE_VERSION}" \
      io.supportguard.component="frontend"
EXPOSE 8080
