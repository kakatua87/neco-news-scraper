-- Neco News v2.0 — Migraciones de base de datos
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- 1. Campo para marcar la noticia portada del día
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS es_portada BOOLEAN DEFAULT FALSE;

-- 2. Campo para la atribución de la imagen (de dónde viene la foto)
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS imagen_fuente TEXT DEFAULT 'Fuente original';

-- 3. Índice para consultas rápidas de portada del día
CREATE INDEX IF NOT EXISTS idx_noticias_portada ON noticias (es_portada, fecha_publicacion DESC)
  WHERE es_portada = TRUE;

-- 4. Índice para el archivo (calendario por fecha)
CREATE INDEX IF NOT EXISTS idx_noticias_fecha_pub ON noticias (fecha_publicacion DESC)
  WHERE estado = 'publicada';
