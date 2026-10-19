FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    RUANG_LAYANAN_DATABASE=/data/notifications.db \
    RUANG_LAYANAN_PUBLIC_DEPLOYMENT=1 \
    RUANG_LAYANAN_COOKIE_SECURE=1

RUN mkdir -p /data

COPY server.py index.html penerima.html install.js service-worker.js manifest.webmanifest ./
COPY assets ./assets

EXPOSE 8000

CMD ["python", "server.py"]
