FROM python:3.13-slim

WORKDIR /app

# Install dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ api/
COPY core/ core/
COPY ai/ ai/
COPY db/ db/

# Run as a non-root user. Nothing here needs root.
RUN useradd --create-home --uid 10001 sagar \
    && chown -R sagar:sagar /app
USER sagar

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
