FROM python:3.12-slim
WORKDIR /app
COPY src ./src
COPY suites ./suites
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
CMD ["python", "-m", "dqmon", "demo"]
