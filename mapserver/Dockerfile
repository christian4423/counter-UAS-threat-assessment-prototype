FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# GDAL/PROJ is the spatial backbone; mapserver-bin ships mapserv + libmapserver,
# python3-mapscript is the matching Python binding (must come from apt, not pip,
# since it's compiled against the system mapserver/GDAL libs).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdal-bin \
    python3-gdal \
    mapserver-bin \
    python3-mapscript \
    python3-pip \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py .
COPY mapfiles ./mapfiles

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
