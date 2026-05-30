FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

WORKDIR /app

# Copia y instala dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia el scraper
COPY scraper.py .

# Comando por defecto (Railway lo sobreescribe con el cron)
CMD ["python", "scraper.py"]
