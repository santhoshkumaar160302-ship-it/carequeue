# syntax=docker/dockerfile:1

# 1) Build the React app.
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build -- --outDir /ui/dist

# 2) Python runtime that serves the API and the built app.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY carequeue ./carequeue
COPY scripts ./scripts
COPY --from=ui /ui/dist ./carequeue/static
ARG GIT_SHA=dev
ENV CQ_BUILD_SHA=$GIT_SHA
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown app /app/data
USER app
ENV DATABASE_URL=sqlite:////app/data/carequeue.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health', timeout=4)"
CMD ["sh", "-c", "uvicorn carequeue.app:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers"]
