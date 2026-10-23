-- Neco News v16.0 — Predicción de sección/prioridad en la Bandeja y salud de las fuentes. Aplicada en Supabase.
--  * noticias.seccion_prevista: sección a la que iría el grupo (la calcula el scraper al ingresarlo).
--  * noticias.prioridad: 1 alta (Local, Policiales, Política, Economía, Zona) · 2 media · 3 descartable.
--  * noticias.motivo_descarte: Espectáculos / Archivo histórico / Nota comercial.
--    Las tres columnas quedan fuera de la lista blanca de `anon` (solo las lee el panel admin).
--  * fuentes_salud: una fila por fuente y corrida del scraper (tarjetas, nuevas, guardadas, bloqueadas, error).
--    Sin acceso público; la escribe y lee solo service_role.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS seccion_prevista text;
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS prioridad smallint;
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS motivo_descarte text;
CREATE TABLE IF NOT EXISTS fuentes_salud (
  id bigserial PRIMARY KEY,
  fuente text NOT NULL,
  ts timestamptz NOT NULL DEFAULT now(),
  tarjetas int DEFAULT 0, nuevas int DEFAULT 0, guardadas int DEFAULT 0,
  sin_cuerpo int DEFAULT 0, bloqueadas int DEFAULT 0, error text
);
CREATE INDEX IF NOT EXISTS fuentes_salud_ts_idx ON fuentes_salud (ts DESC);
ALTER TABLE fuentes_salud ENABLE ROW LEVEL SECURITY;
