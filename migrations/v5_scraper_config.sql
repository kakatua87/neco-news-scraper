-- Neco News v5.0 — Control del scraper desde el panel admin
-- Ejecutar en Supabase SQL Editor (Dashboard → SQL Editor → New query)

CREATE TABLE IF NOT EXISTS scraper_config (
  id INTEGER PRIMARY KEY DEFAULT 1,
  activo BOOLEAN NOT NULL DEFAULT FALSE,
  fuentes_activas TEXT[] NOT NULL DEFAULT ARRAY['nden', 'diarionecochea', 'diario4v', 'tsn', 'diarionq', 'elecos'],
  fecha_inicio TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT scraper_config_single_row CHECK (id = 1)
);

INSERT INTO scraper_config (id, activo)
VALUES (1, TRUE)
ON CONFLICT (id) DO NOTHING;
