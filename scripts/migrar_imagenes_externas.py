"""
Copia a nuestro Storage las imágenes de notas publicadas que todavía apuntan a otro dominio.

Uso (desde neco-news-scraper/, con el .env cargado):
    python scripts/migrar_imagenes_externas.py            # solo lista, no cambia nada
    python scripts/migrar_imagenes_externas.py --apply    # descarga, limpia metadatos, sube y actualiza

Cada imagen se descarga una vez, se re-codifica sin metadatos (ImageHandler) y se sube a
noticias-imagenes/noticias/{id}.{ext}. Si la descarga falla, la nota se deja como está y se
informa: el portal ya oculta al público cualquier imagen_url que no sea de nuestro Storage.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from image_handler import ImageHandler  # noqa: E402
from supabase_client import SupabaseNewsClient  # noqa: E402


def main() -> int:
    aplicar = "--apply" in sys.argv
    cliente = SupabaseNewsClient()
    filas = (
        cliente.client.table("noticias")
        .select("id, titulo, imagen_url")
        .eq("estado", "publicada")
        .not_.is_("imagen_url", "null")
        .execute()
        .data
        or []
    )
    prefijo = f"{config.SUPABASE_URL}/storage/v1/object/public/"
    externas = [f for f in filas if not f["imagen_url"].startswith(prefijo)]
    print(f"Publicadas con imagen: {len(filas)} | externas: {len(externas)}")

    handler = ImageHandler(cliente)
    ok = fallo = 0
    for f in externas:
        dominio = f["imagen_url"].split("/")[2] if "//" in f["imagen_url"] else "?"
        print(f"- id={f['id']} dominio={dominio} titulo={f['titulo'][:50]!r}")
        if not aplicar:
            continue
        nueva = handler.upload_image(f["imagen_url"], str(f["id"]))
        if nueva:
            cliente.update_imagen(str(f["id"]), nueva)
            ok += 1
        else:
            fallo += 1
            print("  ! no se pudo migrar, queda sin cambios")

    if aplicar:
        print(f"Migradas: {ok} | fallidas: {fallo}")
    else:
        print("Modo simulación. Repetí con --apply para migrar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
