# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Frontend image: Vite production build served by Caddy, which also
# reverse-proxies /api, /health and /pipeline to the api service.
#
# Build context: ./frontend. VITE_API_BASE is intentionally EMPTY so the
# React app calls the API same-origin (Caddy proxies it) — no CORS needed.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /src

COPY package.json package-lock.json ./
RUN npm ci

# VITE_API_BASE="" → same-origin API calls through Caddy (see src/api.ts).
ENV VITE_API_BASE=""
COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /src/dist /srv/dist
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 80
