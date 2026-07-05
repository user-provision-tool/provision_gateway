# provision-mcp Dockerfile
FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx python-jose[cryptography]

COPY server.py ./

EXPOSE 8780
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8780"]
