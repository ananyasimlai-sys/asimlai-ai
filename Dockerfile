# Recipe Google Cloud Run uses to package and start the app.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TRUST_PROXY=1
# Work on a local database file and back it up to DATABASE (the storage bucket) after each change.
ENV WORK_ON_LOCAL_COPY=1
# One worker, because the data lives in a single SQLite file.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 "app:create_app()"
