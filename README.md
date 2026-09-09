# Neco News Scraper

Pipeline automatizado de noticias para [Neco News](https://neco-news.vercel.app).

## Arquitectura

**Fase 1 — scraping (sin IA).** `pipeline_scraping()`: scrapea 6 portales de Necochea,
deduplica por similitud de título y guarda los grupos en Supabase con `estado=raw`, más un
push al portal. Corre en **GitHub Actions** (`.github/workflows/scrape.yml`, cron cada 30 min).

**Fase 2 — reescritura con IA (on-demand).** Desde `/admin` del portal se dispara
`POST /procesar-grupo` contra este servicio: lee las notas `raw` del grupo, la IA
(Groq/Gemini/OpenAI/Claude) sintetiza una nota y la deja en `estado=pendiente` para revisión.
Vive en un **web service de Render** (puede estar en Free: se despierta con el request del panel).

**Servicios + limpieza (sin IA).** Farmacias de turno, obituarios y expiración de notas
viejas. Corre en GitHub Actions (`.github/workflows/services.yml`, cron diario).

> El scheduling **no** vive más en el proceso (APScheduler). En Render Free el servicio se
> duerme a los 15 min y los cron in-process no disparaban. Para reactivarlos en un host 24/7
> real (Fly, Render pago): `ENABLE_INPROCESS_SCHEDULER=true`.

## Fuentes

`nden.com.ar`, `tsnnecochea.com.ar`, `diarionq.com.ar`, `elecos.com.ar` → HTTP directo.
`diarionecochea.com`, `diario4v.com` → detrás de WAF; se scrapean vía **proxy rotativo**
(Webshare). El proxy se usa **solo** para esos dominios (`config.PROXY_ONLY_DOMAINS`) para
no agotar el plan free (1 GB/mes); las otras 4 fuentes siempre van directo.

## Desarrollo local

```bash
pip install -r requirements.txt
python -m playwright install chromium

# .env con al menos SUPABASE_URL, SUPABASE_KEY (+ PORTAL_URL, INTERNAL_API_SECRET
# para el push; + AI_API_KEY / GROQ_API_KEY solo si vas a probar la Fase 2)

python main.py --scrape      # corre Fase 1 una vez y termina (no requiere IA)
python main.py --services     # corre servicios (farmacias/obituarios) y termina
python main.py --smoke        # test de conectividad con la IA
python main.py                # levanta la API FastAPI (Fase 2 + endpoints)
```

## GitHub Actions

Dos workflows, disparables a mano desde la pestaña **Actions** (`workflow_dispatch`):

| Workflow | Cron (UTC) | Qué hace |
|---|---|---|
| `scrape.yml` | `*/30 * * * *` | `python main.py --scrape` (instala Chromium) |
| `services.yml` | `0 10 * * *` | `python main.py --services` + limpieza |

**Secrets** (repo → Settings → Secrets and variables → Actions), con los mismos valores que
hoy en Render → Environment:

- `SUPABASE_URL`, `SUPABASE_KEY` — ambos workflows. **`SUPABASE_KEY` tiene que ser la
  `service_role` key** (la misma que usa Render): con la `anon`/publishable, RLS rechaza los
  `insert` en `noticias` con `401 / 42501` y — ojo — `pipeline_scraping` traga esos errores,
  así que el workflow igual sale **verde** sin haber guardado nada. Verificá siempre que
  aparezcan filas nuevas, no solo que el run termine OK.
- `PORTAL_URL`, `INTERNAL_API_SECRET` — solo `scrape.yml` (push al portal).
- `PROXY_ENABLED`, `PROXY_LIST`, `PROXY_USERNAME`, `PROXY_PASSWORD` — solo `scrape.yml`.
  `PROXY_LIST` = `host:puerto,host:puerto` **sin esquema**.

Notas:
- El cron de Actions es *best-effort*: puede demorarse 5–15 min o saltear una ventana en
  horas pico.
- Actions **se auto-desactiva a los 60 días** sin actividad en el repo. Mitigar con un commit
  o un `workflow_dispatch` manual al menos una vez por mes.
- El repo es público → minutos de Actions gratis e ilimitados.

## Proveedores de IA (Fase 2)

| Proveedor | Variable |
|---|---|
| **Groq** (default, gratis) | `AI_PROVIDER=groq` |
| Google Gemini (gratis, rota hasta 3 keys) | `AI_PROVIDER=gemini` |
| OpenRouter | `AI_PROVIDER=openrouter` |
| OpenAI | `AI_PROVIDER=openai` |
| Claude | `AI_PROVIDER=anthropic` |

## Endpoints (Render — Fase 2 + control)

- `GET /health` — health check.
- `GET /stats` — publicadas/pendientes/descartadas *(requiere `INTERNAL_API_SECRET`)*.
- `GET /ai-providers` — proveedores de IA configurados *(idem)*.
- `POST /procesar-grupo` — dispara la Fase 2 para un grupo *(idem)*.
- `POST /run` — corre `pipeline_scraping()` a mano *(idem; necesita Chromium en la imagen)*.
- `POST /run-services`, `POST /limpieza` — *(idem)*.

## Deploy en Render (solo Fase 2)

1. Web Service → conectar repo.
2. Build: `pip install -r requirements.txt && playwright install --with-deps chromium`
   (Chromium sigue haciendo falta solo si vas a usar `POST /run` como fallback manual).
3. Start: `python main.py`.
4. Variables de entorno del `.env` (incluida la key de IA — la Fase 2 la necesita).
5. **No** setear `ENABLE_INPROCESS_SCHEDULER` (el scheduling vive en Actions).

`fly.toml` / `MIGRACION_FLY.md` quedan como plan B pago (host 24/7 con
`ENABLE_INPROCESS_SCHEDULER=true`); ya no son necesarios si Actions cubre el scheduling.
