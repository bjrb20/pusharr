FROM python:3.13-slim
LABEL org.opencontainers.image.title="Pusharr" \
      org.opencontainers.image.description="Web push notifications for Jellyfin, Seerr, Sonarr and Radarr" \
      org.opencontainers.image.source="https://github.com/bjrb20/pusharr" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="bjrb20"
# rsvg-convert: makes the icon set from an uploaded SVG
RUN apt-get update -qq && apt-get install -qq -y --no-install-recommends librsvg2-bin >/dev/null \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY static/ static/
COPY tools/ tools/
COPY tests/ tests/
COPY config.example.yaml .
USER 1000:1000
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data CONFIG_DIR=/config
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "app/server.py"]
