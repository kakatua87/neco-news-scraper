-- Neco News v8.0 — Envíos ciudadanos (botón flotante de WhatsApp/web)
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

-- Guarda los avisos/datos/fotos/videos/PDFs que manda la gente desde el
-- formulario web guiado. El admin los revisa, edita y opcionalmente los
-- convierte en una noticia (tabla `noticias`) vía IA.
CREATE TABLE IF NOT EXISTS envios_ciudadanos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre text,
  telefono text,
  categoria text NOT NULL,
  mensaje text NOT NULL,
  archivos jsonb NOT NULL DEFAULT '[]'::jsonb,
  estado text NOT NULL DEFAULT 'nuevo'
    CHECK (estado IN ('nuevo', 'en_revision', 'procesada', 'descartada')),
  borrador jsonb,
  noticia_id uuid REFERENCES noticias(id) ON DELETE SET NULL,
  origen text NOT NULL DEFAULT 'web',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS envios_ciudadanos_created_at_idx
  ON envios_ciudadanos (created_at DESC);

CREATE INDEX IF NOT EXISTS envios_ciudadanos_estado_idx
  ON envios_ciudadanos (estado);

-- RLS: cualquiera puede crear un envío (insert), nadie anónimo puede leer,
-- editar ni borrar. El panel admin usa el service role (bypassa RLS).
ALTER TABLE envios_ciudadanos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "envios_ciudadanos_insert_publico" ON envios_ciudadanos;
CREATE POLICY "envios_ciudadanos_insert_publico"
  ON envios_ciudadanos
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- Bucket de Storage para los adjuntos (imágenes, video, PDF) de los envíos.
-- Separado de `noticias-imagenes` porque acá se aceptan tipos más pesados.
INSERT INTO storage.buckets (id, name, public)
VALUES ('tips-ciudadanos', 'tips-ciudadanos', true)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "tips_ciudadanos_insert_publico" ON storage.objects;
CREATE POLICY "tips_ciudadanos_insert_publico"
  ON storage.objects
  FOR INSERT
  TO anon
  WITH CHECK (bucket_id = 'tips-ciudadanos');

DROP POLICY IF EXISTS "tips_ciudadanos_lectura_publica" ON storage.objects;
CREATE POLICY "tips_ciudadanos_lectura_publica"
  ON storage.objects
  FOR SELECT
  TO anon
  USING (bucket_id = 'tips-ciudadanos');
