-- Neco News v9.0 — Redacción propia (editor con IA) + separación en Pendientes
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Marca el origen de cada noticia para poder separar, dentro de "Pendientes",
-- las que redactó el propio staff de las que vienen del scraper o de un
-- envío ciudadano.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS origen text NOT NULL DEFAULT 'scraper';

-- Borradores del editor de redacción propia. Cada uno puede guardarse y
-- retomarse antes de mandarlo a la IA para generar la nota final.
CREATE TABLE IF NOT EXISTS borradores_redaccion (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  titulo text NOT NULL DEFAULT '',
  contenido_html text NOT NULL DEFAULT '',
  imagen_portada_url text,
  seccion text NOT NULL DEFAULT 'Local',
  estado text NOT NULL DEFAULT 'borrador'
    CHECK (estado IN ('borrador', 'procesado')),
  autor_email text,
  noticia_id uuid REFERENCES noticias(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS borradores_redaccion_updated_at_idx
  ON borradores_redaccion (updated_at DESC);

-- Sin RLS pública: el editor de redacción es 100% interno, el panel admin
-- accede siempre vía service role (bypassa RLS igual que el resto del panel).
ALTER TABLE borradores_redaccion ENABLE ROW LEVEL SECURITY;
