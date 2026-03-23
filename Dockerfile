FROM python:3.12-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# coordinator serves on 9123 — bound to WireGuard IP by the app itself
EXPOSE 9123

CMD ["datacenter-manager"]
