# Build from the repo root so the classifier code can be shared, not copied by hand:
#   docker build --platform linux/amd64 -f worker/Dockerfile -t strobe-worker .
FROM python:3.12-slim
WORKDIR /app
COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY classifier/handler.py worker/worker.py ./
ENV AWS_REGION=ap-southeast-2 AWS_DEFAULT_REGION=ap-southeast-2 PYTHONUNBUFFERED=1
# exec form: python is PID 1 and receives ECS's SIGTERM directly
CMD ["python", "worker.py"]
