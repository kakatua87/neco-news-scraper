-- Neco News v17.0 - fuentes_salud.viejas: notas descartadas por no ser del dia. Aplicada en Supabase.
ALTER TABLE fuentes_salud ADD COLUMN IF NOT EXISTS viejas int DEFAULT 0;
