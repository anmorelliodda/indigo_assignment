"""Replay saved search labels. Paid inference requires an explicit --online flag.

No chat, SOAP, live stock/budget or returns. Results are local, not Git artifacts.
"""
import argparse
import asyncio
import collections
import json
import random
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from dotenv import load_dotenv
from catalog import Catalog
from models import ProductFilters
from retrieval import SearchEngine

async def run(args):
    queries=[json.loads(s) for s in (ROOT/'tests/fixtures/search-benchmark.jsonl').read_text(encoding='utf-8').splitlines()]
    if args.group:queries=[q for q in queries if q['group'] in args.group]
    if not args.online:
        print(json.dumps({'queries':len(queries),'groups':dict(collections.Counter(q['group'] for q in queries)),
                          'note':'No inference. Add --online to authorize paid API calls; default limit is 20 queries.'},indent=2))
        return
    if not args.output.resolve().is_relative_to((ROOT/'assignment').resolve()):
        raise SystemExit('Write inference reports under ignored assignment/, not to a public fixture.')
    if args.env_file:load_dotenv(args.env_file)
    else:load_dotenv(ROOT/'.env')
    random.Random(20261008).shuffle(queries);queries=queries[:args.limit]
    cat=Catalog(ROOT/'data/gift-shop-catalog.csv');results=[]
    async with httpx.AsyncClient(timeout=30) as client:
        engine=SearchEngine(cat,client,ROOT/'data/search-vectors.json')
        for q in queries:
            row={**q,'methods':{}}
            for mode in args.mode:
                scores=await engine.rank(ProductFilters(query=q['query'],mode=mode))
                top=list(scores)[:10]
                row['methods'][mode]={'top10':[{'id':pid,'score':scores[pid]} for pid in top],
                    'hit1':bool(top and top[0] in q['gold_ids']) if q['scored'] else None}
            results.append(row)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in results)+'\n',encoding='utf-8')
            print(q['id'],{m:r['hit1'] for m,r in row['methods'].items()},flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online',action='store_true')
    parser.add_argument('--limit',type=int,default=20)
    parser.add_argument('--mode',nargs='+',choices=['semantic','hybrid','jev'],default=['semantic'])
    parser.add_argument('--group',action='append')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output',type=Path,default=ROOT/'assignment/search-evaluation/results.jsonl')
    options=parser.parse_args()
    if not 1<=options.limit<=6000:parser.error('limit must be 1..6000; Jev per-candidate inference can incur substantial cost at large limits')
    asyncio.run(run(options))
