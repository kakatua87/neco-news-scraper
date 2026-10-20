-- Neco News v12.0 — Texto de las fuentes de cada nota automática.
-- Aplicada en Supabase (migración v12_texto_fuentes).
--
-- pipeline_ia pisa el cuerpo de la nota líder con la redacción de la IA, así que el texto original de las
-- fuentes se perdía y no se podía contrastar después (rutina de revisión de pendientes). La edición
-- automática lo guarda acá. La columna no está en la lista blanca de columnas públicas: anon no la lee.
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS texto_fuentes text;
