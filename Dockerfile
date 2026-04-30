FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias del sistema para Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar browsers de Playwright
RUN playwright install --with-deps chromium

# Copiar código fuente
COPY . .

# Exponer puerto
EXPOSE 8000

# Ejecutar
CMD ["python", "main.py"]
