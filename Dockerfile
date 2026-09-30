FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Headless Chromium (and the system libraries it needs) for JavaScript careers pages
RUN python -m playwright install --with-deps chromium
COPY app ./app
COPY static ./static
ENV DB_PATH=/app/data/thewatcher.db
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
