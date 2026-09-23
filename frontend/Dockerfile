FROM node:20-alpine AS deps
WORKDIR /srv/app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /srv/app
COPY --from=deps /srv/app/node_modules ./node_modules
COPY . .
# Baked in at build time - Next.js inlines NEXT_PUBLIC_* vars into the client bundle,
# so this must be the URL the *browser* can reach, not the internal docker network.
# Empty = same-origin (recommended): the browser calls /api/* on this server, which
# proxies to BACKEND_INTERNAL_URL (see next.config.mjs). Set NEXT_PUBLIC_API_URL only
# to point the browser at a separately hosted API.
ARG NEXT_PUBLIC_API_URL=
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
ARG BACKEND_INTERNAL_URL=http://retail_copilot_backend:8000
ENV BACKEND_INTERNAL_URL=${BACKEND_INTERNAL_URL}
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /srv/app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=builder /srv/app/public ./public
COPY --from=builder --chown=node:node /srv/app/.next ./.next
COPY --from=builder /srv/app/node_modules ./node_modules
COPY --from=builder /srv/app/package.json ./package.json

USER node
EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
  CMD node -e "fetch('http://localhost:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npm", "start"]
