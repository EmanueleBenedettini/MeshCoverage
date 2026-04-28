FROM python:3.11-slim

# System dependencies for GDAL / rasterio
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gdal-bin \
    libgeos-dev \
    libproj-dev \
    protobuf-compiler \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies before code (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy sources
COPY . .

# Generate protobuf
RUN chmod +x scripts/generate_proto.sh && ./scripts/generate_proto.sh || true

# Data directory (mounted as volume in production)
RUN mkdir -p data/nodes data/dem data/coverage data/heatmaps data/links

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "meshcoverage.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
