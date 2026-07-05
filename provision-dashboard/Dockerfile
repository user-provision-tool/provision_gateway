# provision-dashboard Dockerfile
#
# Build with proxy:
#   docker build --build-arg HTTP_PROXY=http://proxy:port --build-arg HTTPS_PROXY=http://proxy:port .

# Stage 1: Build React app
FROM node:20-alpine AS builder

# Build-time proxy support
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

WORKDIR /app

COPY package.json ./
RUN npm install

COPY tsconfig.json vite.config.ts index.html ./
COPY src/ ./src/
COPY public/ ./public/

RUN npm run build

# Stage 2: Serve with nginx
FROM nginx:alpine

# Copy built files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
