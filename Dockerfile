FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 payflow \
    && useradd --uid 10001 --gid payflow --no-create-home payflow
COPY --chown=payflow:payflow payflow ./payflow
COPY --chown=payflow:payflow web ./web
RUN mkdir -p /app/data && chown payflow:payflow /app/data
USER payflow
ENV DATABASE_URL=sqlite:////app/data/payflow.db
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "payflow.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
