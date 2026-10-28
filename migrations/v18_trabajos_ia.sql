-- Neco News v18.0 - trabajos_ia: registro de lo que el scraper esta procesando con IA (por grupo o una edicion entera).
-- Sirve para avisar (Telegram + campana del admin) si un trabajo quedo a medias porque el servicio se reinicio.
-- Sin acceso para anon/authenticated: solo service role.
CREATE TABLE IF NOT EXISTS trabajos_ia (
  id bigserial PRIMARY KEY,
  origen text NOT NULL,                 -- 'bandeja' (procesar un grupo) | 'edicion' (matutina o manual)
  grupo_id text,
  detalle jsonb,
  estado text NOT NULL DEFAULT 'procesando',  -- procesando | ok | error | interrumpido
  error text,
  inicio timestamptz NOT NULL DEFAULT now(),
  fin timestamptz
);
CREATE INDEX IF NOT EXISTS trabajos_ia_estado_idx ON trabajos_ia (estado, inicio DESC);
ALTER TABLE trabajos_ia ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON trabajos_ia FROM anon, authenticated;
GRANT ALL ON trabajos_ia TO service_role;
GRANT USAGE, SELECT ON SEQUENCE trabajos_ia_id_seq TO service_role;
