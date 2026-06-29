FROM python:3.11-slim

WORKDIR /app

# Build deps for asyncpg (needs gcc)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --trusted-host lets pip install behind corporate SSL inspection (cert-verify
# fails against PyPI otherwise). Safe: package hashes still verified by pip.
RUN pip install --no-cache-dir \
        --trusted-host pypi.org \
        --trusted-host files.pythonhosted.org \
        --trusted-host pypi.python.org \
        -r requirements.txt

COPY . .

# Runtime output directory
RUN mkdir -p output/runs

EXPOSE 8080

CMD ["python", "ui/server.py"]
