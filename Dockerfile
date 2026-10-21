FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GOVERN_ROOT=/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY pyproject.toml .
RUN pip install --no-cache-dir --no-deps .
COPY web ./web
COPY policies ./policies
EXPOSE 8080
CMD ["python", "-m", "govern"]

