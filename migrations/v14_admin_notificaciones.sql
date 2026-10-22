-- Neco News v14.0 — Avisos del panel admin (campana). Aplicada en Supabase (v14_admin_notificaciones).
-- El scraper escribe una fila cuando termina una edición (matutina o manual) con el resumen de lo publicado.
-- Sin acceso para anon/authenticated: solo service role (el portal la lee con el cliente admin).
CREATE TABLE IF NOT EXISTS admin_notificaciones (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  tipo text NOT NULL,
  titulo text NOT NULL,
  detalle jsonb,
  leida boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS admin_notificaciones_ts_idx ON admin_notificaciones (ts DESC);
ALTER TABLE admin_notificaciones ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON admin_notificaciones FROM anon, authenticated;
GRANT ALL ON admin_notificaciones TO service_role;
GRANT USAGE, SELECT ON SEQUENCE admin_notificaciones_id_seq TO service_role;
