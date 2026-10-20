import sys
sys.path.insert(0, r"G:\NECO NEWS\neco-news-scraper")
from supabase_client import SupabaseNewsClient
client = SupabaseNewsClient()
resp = client.client.table("noticias").select("id, titulo, estado").order("created_at", desc=True).limit(5).execute()
print("ULTIMAS NOTICIAS EN SUPABASE:")
for n in resp.data:
    print(f"- {n['titulo'][:30]}... -> ESTADO: {n['estado']}")
