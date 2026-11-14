"""Historical public-record codec. It does not decide whether a person may answer."""
from datetime import datetime
import unicodedata
from .archive import fingerprint
from .knowledge_service import read
SOURCE_POLICY={'enabled':True,'freshness_seconds':86400,'min_interval_seconds':1,'store_body':True}
def key(subject,predicate):
    from .knowledge_lexical import ChineseText
    text=ChineseText()
    return fingerprint([text.simplify(unicodedata.normalize('NFKC',str(x))).strip().casefold() for x in (subject,predicate)])
def instant(value):return datetime.fromisoformat(value).timestamp() if value else None
def source_policy(db,source_id):return read(db,'source_policy.'+source_id,SOURCE_POLICY)['data']
def enabled(*args,**kwargs):return True
def search(*args,**kwargs):return []
def eligible(*args,**kwargs):return []
def merge(base,overlay):return base
