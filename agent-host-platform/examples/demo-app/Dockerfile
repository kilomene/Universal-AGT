# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app
COPY server.py ./

# The worker maps the manifest's service.port to this container port.
EXPOSE 3000
ENV PORT=3000 APP_NAME=demo-app APP_VERSION=1.0.0

CMD ["python3", "server.py"]
