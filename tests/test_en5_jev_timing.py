"""Keep the scene choice deadline bound to the Jev request and receipt."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from experiments.en5_jev_transport import JevTransport
from experiments.en5_scene_route import SceneRouter
from tests.test_en5_scene import assets, response


class Deadline(unittest.IsolatedAsyncioTestCase):
    async def run_route(self, transport):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router = SceneRouter(root/'db',assets(root/'cards'),timeout=.03)
            try:
                choice = await router.resolve('T','S',{},transport,lambda:None,wire_timing=True)
            finally:
                await router.close()
            with router.db() as db:
                row = dict(db.execute('SELECT * FROM routes').fetchone())
            return choice,row

    async def test_connection_and_receipt_processing_do_not_spend_jev_budget(self):
        async def transport(_wire, *, on_dispatch, on_receipt):
            clock = asyncio.get_running_loop().time
            await asyncio.sleep(.07)
            on_dispatch(clock())
            on_receipt(clock())
            await asyncio.sleep(.07)
            return 200,response()
        choice,row = await self.run_route(transport)
        self.assertEqual(choice,('help','ID-help'))
        self.assertEqual(row['reason'],'selected')
        self.assertLess(row['elapsed_ms'],30)

    async def test_slow_jev_receipt_seals_fallback_and_keeps_response(self):
        async def transport(_wire, *, on_dispatch, on_receipt):
            clock = asyncio.get_running_loop().time
            on_dispatch(clock())
            await asyncio.sleep(.07)
            on_receipt(clock())
            return 200,response()
        choice,row = await self.run_route(transport)
        self.assertEqual(choice,('fallback','ID-fallback'))
        self.assertEqual((row['reason'],row['raw']),('timeout',response()))
        self.assertGreaterEqual(row['elapsed_ms'],30)


class Transport(unittest.IsolatedAsyncioTestCase):
    async def test_http_body_boundaries_and_connection_reuse(self):
        async def respond(req):
            self.assertEqual(await req.json(),{'model':'J0'})
            return web.Response(text=response(),headers={'Server-Timing':'fixture;dur=1'})
        app = web.Application()
        app.router.add_post('/systemone',respond)
        async with TestServer(app) as server:
            provider = {'base_url':str(server.make_url('/')),'encrypted_key':'J0','timeout':10}
            client = JevTransport(provider)
            try:
                traces = []
                events = []
                with patch('experiments.en5_jev_transport.unprotect',return_value='J1'):
                    for _ in range(2):
                        status, raw = await client.complete({'model':'J0'},
                            on_dispatch=events.append,on_receipt=events.append,diagnostics=traces.append)
                        self.assertEqual((status,raw),(200,response()))
                self.assertEqual(len(events),4)
                self.assertEqual(sorted(events),events)
                self.assertIsNotNone(traces[0]['connection_ms'])
                self.assertTrue(traces[1]['connection_reused'])
                self.assertEqual(traces[1]['response_headers']['Server-Timing'],'fixture;dur=1')
            finally:
                await client.close()
