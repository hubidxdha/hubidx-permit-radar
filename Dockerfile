FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py .
COPY refresh.py .

CMD ["python", "-u", "scraper.py"]