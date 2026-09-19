# imagegen studio server.
# The image-gen engine is mounted (not baked) so engine updates need no rebuild:
#   docker compose up -d --build
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/
COPY web/ web/

# Engine checkout is mounted at /app/engine (see docker-compose.yml).
ENV IMAGE_GEN_CLI=/app/engine/scripts/image_gen.py \
    IMAGE_GEN_LIBRARY=/data/library \
    PYTHONUTF8=1

EXPOSE 8642
CMD ["python", "scripts/imagegen_server.py", "--host", "0.0.0.0"]
