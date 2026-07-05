FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends openscad && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx trimesh numpy scipy python-multipart networkx lxml
COPY app.py prompts.py parts.py ./
COPY static/ static/
EXPOSE 8093
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8093"]
