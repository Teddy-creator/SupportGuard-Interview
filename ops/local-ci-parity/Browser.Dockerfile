FROM docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS docker_cli

FROM node:22-alpine@sha256:16e22a550f3863206a3f701448c45f7912c6896a62de43add43bb9c86130c3e2

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

RUN set -eux; \
    apk add --no-cache chromium; \
    test "$(node --version)" = "v22.23.1"; \
    docker --version

ENV PLAYWRIGHT_EXECUTABLE_PATH=/usr/bin/chromium

CMD ["sleep", "infinity"]
