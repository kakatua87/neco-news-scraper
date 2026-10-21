-- Neco News v13.0 — Consumo de IA por llamada (panel "Uso de IA"). Aplicada en Supabase (v13_llm_uso).
-- Una fila por solicitud a Gemini, Groq o Claude (incluye las que fallan), atribuida a la nota cuando se conoce.
-- La API key nunca se guarda: solo un alias ("gemini#2 …JcNg"). Sin acceso para anon/authenticated (solo service role).
CREATE TABLE IF NOT EXISTS llm_uso (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  proveedor text NOT NULL,
  key_alias text,
  modelo text,
  proposito text,
  noticia_id uuid,
  grupo_id text,
  tokens_in integer NOT NULL DEFAULT 0,
  tokens_out integer NOT NULL DEFAULT 0,
  ok boolean NOT NULL DEFAULT true,
  codigo integer,
  restante jsonb
);
CREATE INDEX IF NOT EXISTS llm_uso_ts_idx ON llm_uso (ts DESC);
CREATE INDEX IF NOT EXISTS llm_uso_noticia_idx ON llm_uso (noticia_id) WHERE noticia_id IS NOT NULL;
ALTER TABLE llm_uso ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON llm_uso FROM anon, authenticated;
GRANT ALL ON llm_uso TO service_role;
GRANT USAGE, SELECT ON SEQUENCE llm_uso_id_seq TO service_role;

-- Límites de cada modelo y a qué proyecto de Google pertenece cada key (por sus últimos 4 caracteres); editable desde el panel.
ALTER TABLE scraper_config ADD COLUMN IF NOT EXISTS ia_limites jsonb;
