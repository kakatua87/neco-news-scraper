# Neco News Scraper

Pipeline automatizado de noticias para [Neco News](https://neco-news.vercel.app).

## Qué hace

1. **Scrapea** noticias de portales de Necochea (NDEN, Diario Necochea)
2. **Reescribe** con IA (Groq/OpenAI/Claude — configurable)
3. **Guarda** en Supabase como pendiente
4. **Notifica** por Telegram con botones para publicar/descartar

## Setup rápido

```bash
# 1. Instalar dependencias
pip install -r requirements.txt
playwright install chromium

# 2. Configurar .env
# Copiar .env.example → .env y completar AI_API_KEY con tu clave de Groq
# Obtener clave gratis: https://console.groq.com

# 3. Test de conectividad
python main.py --smoke

# 4. Corrida de prueba (una vez)
python main.py --test

# 5. Servidor con scheduler (producción)
python main.py
```

## Proveedores de IA

| Proveedor | Costo | Variable |
|-----------|-------|----------|
| **Groq** (default) | Gratis | `AI_PROVIDER=groq` |
| OpenAI | ~$0.15/1M tokens | `AI_PROVIDER=openai` |
| Claude | ~$3/1M tokens | `AI_PROVIDER=anthropic` |

## Endpoints

- `GET /health` — Health check (para UptimeRobot)
- `GET /stats` — Estadísticas (publicadas/pendientes/descartadas)
- `POST /telegram/callback` — Webhook de Telegram
- `POST /run` — Ejecutar pipeline manualmente

## Deploy en Render

1. Crear Web Service → conectar repo
2. Build Command: `pip install -r requirements.txt && playwright install --with-deps chromium`
3. Start Command: `python main.py`
4. Agregar variables de entorno del `.env`
