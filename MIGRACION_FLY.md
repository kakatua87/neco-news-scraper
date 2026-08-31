# Migrar el scraper de Render a Fly.io

Este servicio corre un scheduler en background (APScheduler) todo el tiempo,
no solo cuando llega un request HTTP — por eso necesita un host que soporte
procesos siempre activos, no una función serverless típica. `fly.toml` ya
está preparado en este repo para eso.

**Nota sobre "gratis":** Fly.io pide tarjeta para crear cuenta y ya no
garantiza un tier 100% gratuito indefinido (varía según cuándo abriste la
cuenta). Con la VM de 1GB que dejamos configurada, el costo esperado para
esta carga (un servicio chico, scrapeando cada 15-60 min) debería ser de
apenas un par de dólares por mes o menos — muy por debajo de lo que cuesta
el overage de ancho de banda en Render. Confirmá el pricing vigente en
fly.io/pricing antes de decidir.

## Pasos

1. **Instalar la CLI** (una sola vez):
   ```bash
   powershell -c "irm https://fly.io/install.ps1 | iex"
   ```
   Reabrí la terminal después de instalar.

2. **Login** (abre el navegador):
   ```bash
   fly auth login
   ```

3. **Crear la app** desde este directorio (usa el `fly.toml` ya incluido,
   `--no-deploy` para no deployar todavía sin las variables de entorno):
   ```bash
   cd neco-news-scraper
   fly launch --no-deploy --copy-config --name neco-news-scraper --region gru
   ```
   Si `neco-news-scraper` ya está tomado por otro usuario de Fly, elegí otro
   nombre (ej. `neco-news-scraper-necochea`) y actualizá `app =` en
   `fly.toml`.

4. **Cargar las variables de entorno** (los mismos valores que hoy están en
   Render → Environment). Reemplazá cada `...` por el valor real:
   ```bash
   fly secrets set \
     SUPABASE_URL=... \
     SUPABASE_KEY=... \
     AI_PROVIDER=... \
     AI_API_KEY=... \
     GROQ_API_KEY=... \
     GEMINI_API_KEY=... \
     GEMINI_API_KEY_2=... \
     GEMINI_API_KEY_3=... \
     OPENROUTER_API_KEY=... \
     TELEGRAM_BOT_TOKEN=... \
     TELEGRAM_CHAT_ID=... \
     PORTAL_URL=https://neco-news-seven.vercel.app \
     SCRAPER_URL=https://neco-news-scraper.fly.dev \
     INTERNAL_API_SECRET=... \
     PROXY_ENABLED=... \
     PROXY_LIST=... \
     PROXY_USERNAME=... \
     PROXY_PASSWORD=...
   ```
   Poné solo las que realmente usás hoy en Render (no hace falta setear las
   que dejaste vacías, como el proxy si no lo usás).

5. **Deployar**:
   ```bash
   fly deploy
   ```
   La primera build tarda varios minutos (instala Chromium de Playwright).

6. **Verificar que levantó**:
   ```bash
   fly status
   fly logs
   curl https://neco-news-scraper.fly.dev/ai-providers
   ```

7. **Apuntar el portal al nuevo scraper**: en Vercel (proyecto `neco-news`)
   actualizá la variable de entorno `SCRAPER_URL` a
   `https://neco-news-scraper.fly.dev` (o el dominio que te haya asignado
   Fly) y redeployá.

8. Una vez confirmado que todo funciona en Fly, podés pausar o borrar el
   servicio en Render para no seguir generando cargos/consumo ahí.

## Si preferís seguir en Render

No hace falta migrar para que vuelva a andar: solo hay que resolver la
suspensión del workspace en el dashboard de Render (agregar tarjeta o subir
de plan) — el fix de ancho de banda que ya subimos (bloqueo de
imágenes/media/fuentes/CSS en Playwright) debería hacer que el consumo baje
mucho y que el límite gratuito dure bastante más.
