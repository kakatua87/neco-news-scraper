import sys
import os
sys.path.append('.')
from supabase_client import SupabaseNewsClient

client = SupabaseNewsClient()
response = client.client.table('noticias').select('id, titulo, estado, created_at').order('created_at', desc=True).limit(20).execute()

print("--- ÚLTIMAS 20 NOTICIAS ---")
for row in response.data:
    print(f"{row['created_at']} | {row['estado']} | {row['titulo'][:60]}")
