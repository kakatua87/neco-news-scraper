-- Neco News v3.0 — Migraciones de base de datos
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- 1. Título-gancho para el kit de Instagram (independiente del titulo editorial y
--    del instagram_text, que es el caption con hashtags)
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS instagram_titulo TEXT;
