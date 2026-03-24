FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default: start the FastAPI server with dashboard
# Override with: docker run scigate python agents/audit_agent.py --path /repo --pretty
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
