"""Bounded zero-model directory leases for a long isolated evaluation run.

Renewal is not model retry: each expired lease gets one check per approved
connection. A failed renewal latches this process, preserves its report, and
requires explicit continuation with a new initial proof. Never stores endpoints,
credentials, catalog bodies or model prompts in evidence.
"""
import asyncio
import json
from pathlib import Path
import time
import math

import aiohttp
from persona_runtime.chat import model_tls_context
from persona_runtime.config import atomic_json
from persona_runtime.private_personas import safe
from persona_runtime.security import unprotect
from scripts.m763_r04 import model_identity
from persona_runtime.provider_compat import endpoint


class DirectoryGate:
    def __init__(self,initial,approved,output):
        self.path=Path(initial);self.approved=approved;self.output=safe(Path(output));self.stopped=False
        self.identities={k:model_identity(approved[k]) for k in ('model','character_selector')}

    def lease_remaining(self,now):
        report=json.loads(self.path.read_text(encoding='utf-8'))
        rows={r['kind']:r for r in report.get('checks',[])}
        if not report.get('normal') or set(rows)!={'main','auxiliary'}:raise ValueError('provider_check_failed')
        left=[]
        for kind,key in (('main','model'),('auxiliary','character_selector')):
            row=rows[kind]
            if not row.get('normal') or row['model_fingerprint']!=self.identities[key]:raise ValueError('provider_check_changed')
            age=now-row['checked_epoch']
            if age<0:raise ValueError('provider_check_clock_invalid')
            left.append(600-age)
        return min(left)

    async def _check(self,kind,key):
        model=self.approved[key];started=time.monotonic()
        row={'kind':kind,'normal':False,'model_calls':0,'checked_epoch':time.time(),'model_fingerprint':self.identities[key]}
        row['phase']='setup';row['directory_timeout_seconds']=20
        trace=aiohttp.TraceConfig()
        async def connecting(*_):row['phase']='connecting'
        async def sent(*_):row['phase']='awaiting_headers'
        trace.on_connection_create_start.append(connecting)
        trace.on_request_headers_sent.append(sent)
        try:
            connector=aiohttp.TCPConnector(ssl=model_tls_context())
            async with aiohttp.ClientSession(connector=connector,timeout=aiohttp.ClientTimeout(total=20,ceil_threshold=math.inf),trust_env=False,trace_configs=[trace]) as http:
                async with http.get(endpoint(model,'models'),
                                    headers={'Authorization':'Bearer '+unprotect(model['encrypted_key'])},allow_redirects=False) as response:
                    row['phase']='reading_body';row['http_status']=response.status;raw=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw)>2*1024*1024:raise ValueError('catalog_capacity')
                    value=json.loads(raw)
                    listed=any(isinstance(item,dict) and item.get('id')==model['name'] for item in value.get('data',[]))
                    row.update(normal=response.status==200 and listed,configured_model_listed=listed)
                    row['phase']='completed'
        except Exception as exc:
            row['failure_type']=type(exc).__name__
        row['elapsed_ms']=round((time.monotonic()-started)*1000,3)
        return row

    async def check_all(self):
        """One GET for an identical connection/model, no duplicate TLS races."""
        rows=[];seen={}
        for kind,key in (('main','model'),('auxiliary','character_selector')):
            model=self.approved[key]
            # Credentials are compared only in process, never persisted/logged.
            identity=(endpoint(model,'models'),unprotect(model['encrypted_key']),model['name']) if model.get('base_url') else (kind,)
            if identity in seen:
                row=dict(seen[identity],kind=kind,model_fingerprint=self.identities[key],reused_from_kind=seen[identity]['kind'])
            else:
                row=await self._check(kind,key);seen[identity]=row
            rows.append(row)
        return rows

    async def ensure(self,min_remaining=30):
        if self.stopped:raise ValueError('provider_wait_after_failed_renewal')
        if self.lease_remaining(time.time())>=min_remaining:return self.path
        rows=await self.check_all()
        report={'normal':all(row['normal'] for row in rows),'model_calls':0,'reason':'expired_directory_lease','checks':rows}
        path=safe(self.output/('provider-check-auto-'+str(time.time_ns())+'.json'),self.output)
        if path.exists():raise ValueError('provider_evidence_exists')
        atomic_json(path,report);self.path=path
        if not report['normal']:
            self.stopped=True
            raise ValueError('provider_wait_after_failed_renewal')
        return self.path
