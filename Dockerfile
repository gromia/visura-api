FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY main.py sister.py province.py ./

RUN useradd -m -u 1000 app && chown -R app /app
USER app

EXPOSE 8000
# rete privata di Railway: IPv6
CMD ["uvicorn", "main:app", "--host", "::", "--port", "8000", "--workers", "1"]
