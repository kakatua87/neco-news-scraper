-- Neco News v10.0 — Edición matutina automática (Fase 0: ranking + log, sin publicar)
-- Ejecutar en Supabase SQL Editor.

ALTER TABLE noticias ADD COLUMN IF NOT EXISTS score numeric;
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS auto_motivo text;

-- Flags y pesos de la edición automática, editables sin deploy.
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS edicion_auto_activa boolean NOT NULL DEFAULT false;
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS edicion_max integer NOT NULL DEFAULT 8;
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS edicion_auto_publicar boolean NOT NULL DEFAULT false;
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS edicion_secciones_excluidas text[] NOT NULL DEFAULT '{Obituarios,Farmacias}';
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS edicion_pesos jsonb;

CREATE TABLE IF NOT EXISTS edicion_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ejecutado_at timestamptz NOT NULL DEFAULT now(),
  modo text NOT NULL,                 -- 'sombra' | 'auto'
  grupo_id uuid,
  noticia_id uuid,
  titulo text,
  seccion text,
  score numeric,
  n_fuentes integer,
  resultado text NOT NULL,            -- 'procesada' | 'omitida' | 'error'
  motivo text
);
CREATE INDEX IF NOT EXISTS edicion_log_ejecutado_idx ON edicion_log (ejecutado_at DESC);
ALTER TABLE edicion_log ENABLE ROW LEVEL SECURITY;
