"""Bounded read-only Wikimedia requests built exclusively from public identifiers."""

from datetime import datetime, timezone
import asyncio
import json
import re
import ssl
import urllib.parse

import certifi
import aiohttp

from .models import RuntimeErrorBase


class FetchError(RuntimeErrorBase):
    def __init__(self, reason, retry_after=60, withdrawn=False):
        super().__init__(reason)
        self.retry_after = retry_after
        self.withdrawn = withdrawn


def public_identifier(source, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise RuntimeErrorBase('knowledge_public_identifier_invalid')
    value = value.strip().replace('_', ' ')
    if source == 'wikidata':
        if not re.fullmatch(r'Q[1-9][0-9]*', value):
            raise RuntimeErrorBase('knowledge_public_identifier_invalid')
    elif source == 'wikipedia_zh':
        namespace = r'^(?:user|user talk|talk|wikipedia|special|file|template|module|category|用户|使用者|用戶|用户讨论|用戶討論|討論|讨论|維基百科|维基百科|特殊|檔案|文件|模板|模組|模块|分類|分类)\s*[:：]'
        if re.search(r'[|<>\[\]{}\r\n\x00-\x1f]', value) or re.search(namespace,value,re.IGNORECASE) or '://' in value:
            raise RuntimeErrorBase('knowledge_public_identifier_invalid')
    else:
        raise RuntimeErrorBase('knowledge_source_unknown')
    return value


class WikimediaFetcher:
    def __init__(self, stopped):
        self.stopped = stopped
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def fetch(self, source, public_id):
        return asyncio.run(self._fetch(source, public_id))

    async def _fetch(self, source, public_id):
        public_id = public_identifier(source['id'], public_id)
        if self.stopped.is_set():
            raise FetchError('knowledge_stopped')
        if source['adapter'] == 'mediawiki':
            parameters = {'action': 'query', 'prop': 'revisions|info', 'titles': public_id,
                'rvprop': 'ids|timestamp|content', 'rvslots': 'main', 'redirects': 1,
                'inprop': 'url', 'meta': 'siteinfo', 'siprop': 'rightsinfo'}
        else:
            parameters = {'action': 'wbgetentities', 'ids': public_id,
                'props': 'info|labels|descriptions|claims', 'languages': 'zh|zh-hans|zh-hant'}
        parameters.update(format='json', formatversion=2)
        if not source.get('interactive', False):
            parameters['maxlag'] = 5
        headers = {'User-Agent': 'PCAR/0.9 public-knowledge-reader '
            '(https://github.com/FoxofCAUC/Persona-Centric-Agent-Runtime)', 'Accept': 'application/json'}
        try:
            timeout=source.get('timeout',10);proxy=None
            hostname=urllib.parse.urlsplit(source['endpoint']).hostname
            if hostname in ('zh.wikipedia.org','www.wikidata.org'):
                from .knowledge_dns import public_target,system_proxy
                try:await public_target(hostname,443,timeout)
                except RuntimeErrorBase:raise FetchError('knowledge_source_dns_unavailable') from None
                proxy=system_proxy()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False) as session:
                async with session.get(source['endpoint'], params=parameters, headers=headers,
                                       ssl=self.ssl_context, allow_redirects=False,proxy=proxy) as response:
                    if response.status != 200:
                        delay = response.headers.get('Retry-After', '60')
                        try:
                            delay = max(1, int(delay))
                        except ValueError:
                            from email.utils import parsedate_to_datetime
                            try:
                                delay = max(1, int((parsedate_to_datetime(delay)
                                    - datetime.now(timezone.utc)).total_seconds()))
                            except (ValueError, TypeError):
                                delay = 60
                        raise FetchError('knowledge_http_' + str(response.status), delay)
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        if self.stopped.is_set():
                            raise FetchError('knowledge_stopped')
                        size += len(chunk)
                        if size > 2 * 1024**2:
                            raise FetchError('knowledge_document_too_large', 86400)
                        chunks.append(chunk)
                    raw = json.loads(b''.join(chunks))
        except FetchError:
            raise
        except (aiohttp.ClientError, OSError, ValueError):
            raise FetchError('knowledge_fetch_unavailable') from None
        if not isinstance(raw,dict):
            raise FetchError('knowledge_source_structure_invalid')
        if 'error' in raw:
            raise FetchError('knowledge_upstream_' + str(raw['error'].get('code', 'error'))[:50])
        try:
            document = self.parse(source, raw)
        except FetchError:
            raise
        except (KeyError,TypeError,ValueError,IndexError):
            raise FetchError('knowledge_source_structure_invalid') from None
        document['metadata']['request_mode'] = 'interactive' if source.get('interactive') else 'background_maxlag_5'
        return document

    @staticmethod
    def parse(source, raw):
        if source['adapter'] == 'mediawiki':
            pages = raw.get('query', {}).get('pages', [])
            if len(pages) != 1 or pages[0].get('missing'):
                raise FetchError('knowledge_missing_unconfirmed', 300)
            page = pages[0]
            if page.get('ns') != 0:
                raise FetchError('knowledge_namespace_rejected', 86400)
            revision = (page.get('revisions') or [{}])[0]
            slot = revision.get('slots', {}).get('main', {})
            if 'texthidden' in revision or 'texthidden' in slot or 'suppressed' in revision:
                raise FetchError('knowledge_upstream_withdrawn', withdrawn=True)
            body = slot.get('content')
            if not isinstance(body, str) or not body.strip():
                raise FetchError('knowledge_content_unavailable')
            return {'upstream_id': str(page['pageid']), 'revision': revision['revid'],
                'title': page['title'], 'body': body, 'upstream_at': revision['timestamp'],
                'url': 'https://zh.wikipedia.org/wiki/' + urllib.parse.quote(page['title'].replace(' ', '_')),
                'metadata': {'parser': 'mediawiki-wikitext-v1', 'rightsinfo': raw['query'].get('rightsinfo'),
                             'license_url': source['license_url']}, 'raw': raw}
        entities = list(raw.get('entities', {}).values())
        if len(entities) != 1 or 'missing' in entities[0]:
            raise FetchError('knowledge_missing_unconfirmed', 300)
        entity = json.loads(json.dumps(entities[0], ensure_ascii=False, sort_keys=True))
        labels = entity.get('labels', {})
        title = next((labels[key]['value'] for key in ('zh', 'zh-hans', 'zh-hant') if key in labels), entity['id'])
        # Preserve the original statement JSON. Values retain their property IDs and references.
        body = json.dumps({'id': entity['id'], 'labels': labels,
            'descriptions': entity.get('descriptions', {}), 'claims': entity.get('claims', {})}, ensure_ascii=False)
        return {'upstream_id': entity['id'], 'revision': entity['lastrevid'], 'title': title,
            'body': body, 'upstream_at': entity.get('modified'),
            'url': 'https://www.wikidata.org/wiki/' + entity['id'],
            'metadata': {'parser': 'wikibase-json-v1', 'license_url': source['license_url'], 'license': 'CC0'},
            'raw': raw}
