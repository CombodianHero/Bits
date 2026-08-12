FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge_to_success_sdk.py .
COPY bot.py .

CMD ["python", "bot.py"]
