-- Neco News v7.0 — Links a las fuentes originales de cada noticia procesada
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Guarda, para cada noticia ya sintetizada por la IA, la lista de fuentes
-- (nombre + URL) que se usaron para escribirla. Antes esta información se
-- perdía: solo se conservaba "fuente" (nombres separados por coma) y
-- "url_original" del líder del grupo; las notas raw secundarias se
-- marcaban como 'descartada' sin dejar rastro de sus URLs en la noticia final.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS fuentes_urls JSONB;
