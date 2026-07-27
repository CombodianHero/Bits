FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bts_scraper_bot.py .

CMD ["python", "bts_scraper_bot.py"]
