-- Neco News v15.0 — Carrusel de portada acumulativo. Aplicada en Supabase (v15_carrusel_acumulativo).
-- Cada lote de notas publicadas (edición matutina, botón "Publicar con IA", rutina de revisión) suma sus 3 notas más
-- relevantes al carrusel; las de los lotes anteriores del día se corren hacia atrás (tope 9) y las de días anteriores salen.
--  * noticias.portada_origen: 'lote' (sumada por un lote) o 'respaldo' (la armó el trigger al publicar si hoy no hay lotes).
--  * agregar_a_carrusel(ids uuid[], tope int default 9): suma las ids (la primera queda primera). Solo service_role.
--  * actualizar_carrusel_portada(): ya no borra lo sumado por lotes; solo vence lo viejo y arma el respaldo si hace falta.
-- Las definiciones completas están en la migración aplicada (ver `list_migrations`).
ALTER TABLE noticias ADD COLUMN IF NOT EXISTS portada_origen text;
