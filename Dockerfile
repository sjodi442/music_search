FROM python:3.11-alpine

LABEL maintainer="sjodi442"
LABEL description="Hi-Res Lossless Music Search Engine and Web Streaming Player"

WORKDIR /app

# Zero external pip packages needed!
COPY bottle.py music_search.py app.py /app/
COPY static /app/static/

ENV HOST=0.0.0.0
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8080/ || exit 1

ENTRYPOINT ["python3", "app.py"]
