FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8080
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY adapter/ adapter/
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['PORT'],timeout=2)"
CMD ["python", "-m", "adapter.server"]
