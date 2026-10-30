FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY config.py auth.py server.py ./
ENV AWS_REGION=ap-southeast-2 PORT=8000
EXPOSE 8000
# shell form so $PORT is expanded: ECS sets it to 8080, the port CAB432SG opens
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT}
