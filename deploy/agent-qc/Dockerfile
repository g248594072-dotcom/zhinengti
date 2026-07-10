FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV QC_CONFIG_DIR=/app/config

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", \
    "--server.address=0.0.0.0", \
    "--server.port=8501", \
    "--server.baseUrlPath=chat-qc", \
    "--server.headless=true"]
