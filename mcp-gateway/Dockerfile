FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

ENV FINANCE_AGENT_BASE_URL=http://fin-portfolio-agent:8101
ENV FINANCE_AGENT_CHAT_PATH=/api/v1/ai/chat
EXPOSE 8120

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8120"]
