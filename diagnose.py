"""
Diagnóstico completo del pipeline de Neco News en Render.
Requiere INTERNAL_API_SECRET en el entorno (mismo valor que en Render/Vercel).
"""
import os
import requests
import json

BASE = "https://neco-news-scraper.onrender.com"
HEADERS = {"Authorization": f"Bearer {os.getenv('INTERNAL_API_SECRET', '')}"}

# 1. Stats actuales
print("=" * 60)
print("1. STATS ACTUALES")
print("=" * 60)
r = requests.get(f"{BASE}/stats", headers=HEADERS)
print(json.dumps(r.json(), indent=2))

# 2. Forzar un pipeline_scraping
print("\n" + "=" * 60)
print("2. EJECUTANDO pipeline_scraping EN RENDER...")
print("=" * 60)
try:
    r = requests.post(f"{BASE}/run", headers=HEADERS, timeout=120)
    print("Respuesta:", json.dumps(r.json(), indent=2))
except Exception as e:
    print(f"ERROR: {e}")

# 3. Stats despues de ejecutar
print("\n" + "=" * 60)
print("3. STATS DESPUES DE EJECUTAR")
print("=" * 60)
r = requests.get(f"{BASE}/stats", headers=HEADERS)
print(json.dumps(r.json(), indent=2))

# 4. Verificar health
print("\n" + "=" * 60)
print("4. HEALTH CHECK")
print("=" * 60)
r = requests.get(f"{BASE}/health")
print(json.dumps(r.json(), indent=2))
