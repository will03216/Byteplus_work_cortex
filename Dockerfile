FROM python:3.11-slim

WORKDIR /app

# Build deps for asyncpg (needs gcc)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime output directory
RUN mkdir -p output/runs

EXPOSE 8080

CMD ["python", "ui/server.py"]
