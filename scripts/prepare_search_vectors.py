"""Explicitly regenerate public catalogue embeddings after metadata changes.

Uses runtime environment secrets. No CSV price/stock, customers, orders or SOAP.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from dotenv import load_dotenv
from catalog import Catalog,metadata_text
from retrieval import fingerprint,unit

async def run(args):
    load_dotenv(args.env_file or ROOT/'.env')
    endpoint=os.getenv('AZURE_EMBEDDING_ENDPOINT','').rstrip('/');key=os.getenv('AZURE_EMBEDDING_KEY','')
    deployment=os.getenv('AZURE_EMBEDDING_DEPLOYMENT','text-embedding-3-small')
    if not endpoint.startswith('https://') or not key:raise SystemExit('Set the embedding HTTPS endpoint/key as private runtime configuration.')
    cat=Catalog(args.catalog);ids=list(cat.products);vectors={}
    async with httpx.AsyncClient(timeout=90) as client:
        for offset in range(0,len(ids),64):
            batch=ids[offset:offset+64]
            r=await client.post(endpoint+'/openai/deployments/'+deployment+'/embeddings?api-version=2024-10-21',
                headers={'api-key':key},json={'input':[metadata_text(cat.details(pid)) for pid in batch],'encoding_format':'float'})
            if r.status_code!=200:raise SystemExit(f'Embedding failed with HTTP {r.status_code}. Existing index untouched; no automatic retry.')
            d=r.json()
            if d['model']!='text-embedding-3-small' or len(d['data'])!=len(batch):raise SystemExit('Unexpected model/response; existing index untouched.')
            for row in d['data']:
                vector=row['embedding'];unit(vector,1536);vectors[batch[row['index']]]=vector
            print('Embedded',len(vectors),'products;',d['usage']['prompt_tokens'],'batch input tokens',flush=True)
    if set(vectors)!=set(ids):raise SystemExit('Incomplete embeddings; existing index untouched.')
    artifact={'schema':1,'model':'text-embedding-3-small','model_version':'1','dimensions':1536,'fingerprint':fingerprint(cat),'vectors':vectors}
    temporary=args.output.with_suffix('.json.tmp');temporary.write_text(json.dumps(artifact,separators=(',',':')),encoding='utf-8');temporary.replace(args.output)
    print('Saved',args.output)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--catalog',type=Path,default=ROOT/'data/gift-shop-catalog.csv')
    p.add_argument('--output',type=Path,default=ROOT/'data/search-vectors.json')
    p.add_argument('--env-file',type=Path)
    asyncio.run(run(p.parse_args()))
