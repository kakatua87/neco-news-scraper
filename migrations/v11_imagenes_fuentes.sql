-- Neco News v11.0 — Fotos de cada fuente para elegir desde Pendientes.
-- Ejecutar en Supabase SQL Editor.
--
-- [{"fuente": "El Ecos", "url": "https://.../noticias/<id>-fuente0.jpg", "con_logo": false}, ...]
-- Las URLs apuntan a copias en el bucket propio (sin metadatos). La columna no está en la lista
-- blanca de COLUMNAS_PUBLICAS, así que queda oculta para el rol anon (datos de origen).
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS imagenes_fuentes jsonb;
