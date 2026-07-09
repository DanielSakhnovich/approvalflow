# Stage 1: build the React SPA.
FROM node:20-alpine AS build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
# the shipped fixtures the submit dropdown loads at runtime
COPY sample-invoices.json ./public/sample-invoices.json
RUN npm run build

# Stage 2: nginx serves the built SPA and proxies /api to the services.
FROM nginx:1.27-alpine
COPY gateway/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 80
