"""Offline mode/provider regression. No paid inference, SOAP or customer data."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from catalog import Catalog
from main import app
from models import ProductFilters
from retrieval import SearchEngine, SearchUnavailable, fingerprint
from test_backend import FakeOrdiNet

ROOT=Path(__file__).resolve().parents[1]


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.catalog=Catalog(ROOT/'data/gift-shop-catalog.csv')
        self.artifact=json.loads((ROOT/'data/search-vectors.json').read_text(encoding='utf-8'))
        self.calls=[];self.fail=None
        def handle(request):
            payload=json.loads(request.content);self.calls.append((request.url.path,payload))
            if self.fail:
                return httpx.Response(self.fail,json={'error':'PRIVATE upstream credential details'})
            if 'embeddings' in request.url.path:
                return httpx.Response(200,json={'model':'text-embedding-3-small','data':[{'index':0,'embedding':self.artifact['vectors']['HL-001']}]})
            return httpx.Response(200,json={'model':'jev-1.13.0','answers':{k:{'noul':.2 if k=='0' else .9} for k in payload['questions']}})
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.engine=SearchEngine(self.catalog,self.client,ROOT/'data/search-vectors.json',endpoint='https://embedding.test',
                                 embedding_key='private-embedding-key',jev_key='private-jev-key')
    async def asyncTearDown(self):
        await self.client.aclose()
    async def test_default_is_semantic_and_scores_cosine(self):
        filters=ProductFilters(query='Una lampada da tavolo')
        self.assertEqual(filters.mode,'semantic')
        result=await self.engine.rank(filters)
        self.assertEqual(next(iter(result)),'HL-001');self.assertAlmostEqual(result['HL-001'],1)
        self.assertEqual(len(self.calls),1)
    async def test_browse_ids_and_empty_candidates_do_not_call_provider(self):
        for filters in (ProductFilters(),ProductFilters(query=''),ProductFilters(product_id=['HL-001'],mode='jev'),
                        ProductFilters(query='a gift',brand=['missing-brand'])):
            result=await self.engine.rank(filters)
            self.assertTrue(all(v is None for v in result.values()))
        self.assertEqual(self.calls,[])
    async def test_mandatory_metadata_filters_before_ranking(self):
        result=await self.engine.rank(ProductFilters(query='lamp',category=['Tech & Gadgets'],subcategory=['Audio']))
        self.assertTrue(result);self.assertNotIn('HL-001',result)
        self.assertTrue(all(self.catalog.details(pid)['subcategory']=='Audio' for pid in result))
    async def test_hybrid_no_zero_lexical_votes_and_shared_embedding_cache(self):
        semantic=await self.engine.rank(ProductFilters(query='zzqnonexistent'))
        hybrid=await self.engine.rank(ProductFilters(query='zzqnonexistent',mode='hybrid'))
        self.assertEqual(list(semantic)[:40],list(hybrid)[:40]);self.assertEqual(len(self.calls),1)
        self.assertEqual(sum(v>0 for v in hybrid.values()),40)
    async def test_concurrent_identical_queries_single_paid_call(self):
        f=ProductFilters(query='a lamp')
        results=await asyncio.gather(*(self.engine.rank(f) for _ in range(8)))
        self.assertEqual(len(self.calls),1);self.assertTrue(all(r==results[0] for r in results))
    async def test_jev_independent_decisions_closed_candidate_scope(self):
        f=ProductFilters(query='a lamp',mode='jev',product_id=['HL-001','HL-002'])
        result=await self.engine.rank(f)
        self.assertEqual(list(result),['HL-002','HL-001'])
        payload=self.calls[0][1]
        self.assertEqual(set(payload['state']['catalog']),{'HL-001','HL-002'})
        self.assertTrue(all(q['type']=='noul' for q in payload['questions'].values()))
        self.assertEqual(payload['model'],'jev-1.13.0')
        self.assertNotIn('gold_ids',json.dumps(payload));self.assertNotIn('price_eur',json.dumps(payload))
        await self.engine.rank(f.model_copy(update={'offset':2,'fields':['name']}))
        self.assertEqual(len(self.calls),1)
    async def test_jev_cache_key_includes_candidate_filters(self):
        await self.engine.rank(ProductFilters(query='lamp',mode='jev',product_id=['HL-001']))
        await self.engine.rank(ProductFilters(query='lamp',mode='jev',product_id=['HL-002']))
        self.assertEqual(len(self.calls),2)
    async def test_provider_failure_safe_no_fallback_no_cached_failure(self):
        self.fail=429
        for _ in range(2):
            with self.assertRaises(SearchUnavailable) as error:await self.engine.rank(ProductFilters(query='lamp'))
            self.assertNotIn('PRIVATE',str(error.exception))
        self.assertEqual(len(self.calls),2)
    async def test_invalid_jev_decision_is_not_ranked(self):
        async def bad(*args):
            return {'model':'jev-1.13.0','answers':{'0':{'noul':1.5}}}
        self.engine._post=bad
        with self.assertRaises(SearchUnavailable):
            await self.engine.rank(ProductFilters(query='lamp',mode='jev',product_id=['HL-001']))
    async def test_negative_cosine_remains_valid(self):
        self.artifact['vectors']['HL-001']=[-v for v in self.artifact['vectors']['HL-001']]
        result=await self.engine.rank(ProductFilters(query='different',product_id=['HL-001']))
        self.assertAlmostEqual(result['HL-001'],-1)
    async def test_missing_keys_fail_closed_only_requested_mode(self):
        self.engine.embedding_key=''
        with self.assertRaises(SearchUnavailable):await self.engine.rank(ProductFilters(query='lamp'))
        await self.engine.rank(ProductFilters(query='lamp',mode='jev'))
        self.assertEqual(len(self.calls),1)
    async def test_private_identifiers_not_sent(self):
        for q in ('find hello@example.com','gift for GS-2026-000117'):
            with self.assertRaises(SearchUnavailable):await self.engine.rank(ProductFilters(query=q))
        self.assertEqual(self.calls,[])
    async def test_index_fingerprint_excludes_stale_price_stock(self):
        original=fingerprint(self.catalog)
        self.catalog.products['HL-001']['price_eur']='999999'
        self.catalog.products['HL-001']['stock']='123456'
        self.assertEqual(fingerprint(self.catalog),original)
        self.catalog.products['HL-001']['description']='changed'
        with self.assertRaises(ValueError):SearchEngine(self.catalog,self.client,ROOT/'data/search-vectors.json')
    async def test_invalid_embedding_rejected(self):
        self.artifact['vectors']['HL-001']=[1,2]
        with self.assertRaises(SearchUnavailable):await self.engine.rank(ProductFilters(query='lamp'))


class RetrievalHttpTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict('os.environ',{'TOOL_API_KEY':'test-key','ORDINET_USER':'test','ORDINET_PASSWORD':'test','RETURNS_ENABLED':'false'})
        self.environment.start();self.client=TestClient(app);self.client.__enter__()
        self.live=FakeOrdiNet();self.live.availability=AsyncMock(side_effect=self.live.availability);app.state.ordinet=self.live
        self.calls=[]
        artifact=json.loads((ROOT/'data/search-vectors.json').read_text(encoding='utf-8'))
        def handle(request):
            payload=json.loads(request.content);self.calls.append(payload)
            if 'embeddings' in request.url.path:
                return httpx.Response(200,json={'model':'text-embedding-3-small','data':[{'index':0,'embedding':artifact['vectors']['HL-001']}]})
            return httpx.Response(200,json={'model':'jev-1.13.0','answers':{k:{'noul':.8} for k in payload['questions']}})
        self.provider=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        app.state.search=SearchEngine(app.state.catalog,self.provider,ROOT/'data/search-vectors.json',endpoint='https://embedding.test',embedding_key='fake',jev_key='fake')
        self.headers={'X-API-Key':'test-key'}
    def tearDown(self):
        self.client.portal.call(self.provider.aclose)
        self.client.__exit__(None,None,None);self.environment.stop()
    def post(self,body):return self.client.post('/products',json=body,headers=self.headers)
    def test_one_tool_mode_default_and_browse(self):
        schema=self.client.get('/openapi.json').json()
        self.assertEqual([v['operationId'] for p in schema['paths'].values() for v in p.values()].count('search_products'),1)
        for mode in ('semantic','hybrid','jev'):
            response=self.post({'query':'a lamp','mode':mode,'fields':['name'],'k':1})
            self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['search_mode'],mode)
            self.assertEqual(set(response.json()['products'][0]),{'product_id','name','relevance_score'})
        self.assertEqual(self.post({'product_id':['HL-001']}).json()['search_mode'],'browse')
    def test_live_budget_not_model_price_and_projection_after_filters(self):
        response=self.post({'query':'a lamp','max_price_eur':30,'fields':['name']})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['products'],[])
        self.assertIsNotNone(response.json()['next_offset'])
        self.live.availability.assert_awaited_once()
    def test_invalid_mode_or_private_query_before_all_upstreams(self):
        for body in ({'mode':'vector','query':'lamp'},{'mode':'bm25','query':'lamp'},
                     {'query':'GS-2026-000117'},{'query':'someone@example.com'}):
            self.assertEqual(self.post(body).status_code,422)
        self.assertEqual(self.calls,[]);self.live.availability.assert_not_awaited()
    def test_missing_secret_safe_503_does_not_call_soap(self):
        app.state.search.embedding_key=''
        response=self.post({'query':'lamp'})
        self.assertEqual(response.status_code,503);self.assertEqual(response.headers['Retry-After'],'5')
        self.live.availability.assert_not_awaited()
    def test_saved_dataset_integrity(self):
        queries=[json.loads(s) for s in (ROOT/'tests/fixtures/search-benchmark.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(queries),5966)
        self.assertEqual(len({q['id'] for q in queries}),5966)
        for q in queries:
            self.assertNotRegex(q['query'],r'[^\s@]+@[^\s@]+|GS-\d{4}-\d{6}')
            self.assertTrue(set(q['gold_ids']).issubset(app.state.catalog.products))
