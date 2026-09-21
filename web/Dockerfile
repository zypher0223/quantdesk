FROM node:22-alpine AS builder
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
RUN apk add --no-cache apache2-utils
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint-auth.sh /docker-entrypoint-auth.sh
RUN chmod 755 /docker-entrypoint-auth.sh
COPY --from=builder /build/dist /usr/share/nginx/html
EXPOSE 80
ENTRYPOINT ["/docker-entrypoint-auth.sh"]
CMD ["nginx", "-g", "daemon off;"]
