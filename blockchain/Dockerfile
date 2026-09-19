# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Hardhat image: local EVM node + AuditAnchor deployer (one-shot).
# Publicly known test accounts only — never holds real keys or funds.
# ---------------------------------------------------------------------------
FROM node:22-alpine
WORKDIR /chain

COPY package.json package-lock.json ./
RUN npm ci

COPY hardhat.config.js ./
COPY contracts ./contracts
COPY scripts ./scripts
COPY test ./test

EXPOSE 8545
CMD ["npx", "hardhat", "node", "--hostname", "0.0.0.0", "--port", "8545"]
