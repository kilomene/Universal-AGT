# syntax=docker/dockerfile:1
FROM node:20-alpine

WORKDIR /app
COPY server.js ./

# The worker maps the manifest's service.port to this container port.
EXPOSE 3000
ENV PORT=3000 NODE_ENV=production

CMD ["node", "server.js"]
