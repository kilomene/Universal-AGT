# syntax=docker/dockerfile:1
FROM nginx:alpine

COPY index.html /usr/share/nginx/html/index.html

# The worker maps the manifest's service.port to this container port.
EXPOSE 80

# nginx:alpine already runs nginx in the foreground; no CMD needed.
