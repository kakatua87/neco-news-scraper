-- Neco News v4.0 — Migraciones de base de datos
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Permite ocultar/descartar noticias del kit de Instagram (una a una o en bloque)
-- sin afectar la noticia publicada en el portal.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS instagram_descartado BOOLEAN DEFAULT FALSE;
