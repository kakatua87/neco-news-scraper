-- Neco News v6.0 — Carrusel de portada (varias noticias destacadas rotando)
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- es_portada ya existe y ahora significa "incluida en el carrusel de portada".
-- orden_portada define el orden de rotación entre las incluidas.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS orden_portada INTEGER;
