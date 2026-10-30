FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev
COPY --from=build /app/dist ./dist
ENV AWS_REGION=ap-southeast-2 AGENT_ACP_PORT=3000
EXPOSE 3000
CMD ["node", "dist/server.js"]
