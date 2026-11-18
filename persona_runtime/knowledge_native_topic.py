"""Local subject matching for previously acquired public material."""
import re
from urllib.parse import urlsplit

from .archive_query import lexical, normal
from .recall_runtime import FUNCTION_CHARS


# Common conversational words carry no independent topical evidence.
COMMON_HAN = re.compile(r'问题|情况|内容|东西|方式|信息|资料|结论|结果|版本|功能|技术|之前|之后|当前|现在|怎么说|怎么|什么|哪些')
COMMON_ASCII = frozenset(
    'the this that these those there here and not with from into for about '
    'what when where which please explain can how use using previous latest new '
    'version versions model models info information details public pro api app http https www '
    'you your like help need support issue question search find work more are have has'.split()
)
FUNCTION_HAN = FUNCTION_CHARS | set('不没别非无些吗从谁')
URL = re.compile(r'https?://[^\s<>()（）\[\]，。！？；、“”「」]+', re.IGNORECASE)
HAN_PAIR = re.compile(r'(?=([\u3400-\u9fff]{2}))')
FOLLOWUP = re.compile(
    r'^(?:那|所以|嗯[,， ]*)?(?:就)?(?:是|就是|也|确实|看起来|看来)?(?:差不多|这样|那样|'
    r'原来如此|原来是这样|明白了|懂了|接着说|继续说|再说一遍|具体呢|然后呢|再查一处细节)'
    r'[的了吧嘛啊呢呀喽～~!！。?？\s]*$'
    r'|^(?:就|还是|先)?(?:按|用|根据)(?:你)?(?:刚才说的|刚查到的|刚查的|更正后的资料|已有资料|旧资料)'
)
TOPIC_BREAK = re.compile(r'换个话题|聊点别的|不再采用|先不采用|不聊这个')


def is_followup(text):
    text = normal(text).strip()
    return not TOPIC_BREAK.search(text) and FOLLOWUP.search(text) is not None


class TopicQuery:
    def __init__(self, question):
        self.urls={url for match in URL.finditer(question) if (url:=_literal_url(match.group())) is not None}
        self.text=_clean(question)
        self.names=_anchors(self.text)
        self.pairs=[(m.start(),m.group(1)) for m in HAN_PAIR.finditer(self.text) if _han_anchor(m.group(1))]

    def score(self, *, previous_question, titles, urls, notes):
        """Compare the current subject with the acquisition, not the copied results."""
        if self.urls.intersection(_literal_url(url) for url in urls):
            return 3
        labels=[_clean(text) for text in (previous_question,*titles)]
        has_name=bool(self.names)
        exact_short=len(self.text)==2 and _han_anchor(self.text)
        for label in labels:
            if (has_name and self.names.intersection(_anchors(label))) or (exact_short and self.text==label):
                return 2
        if len(self.pairs)<2:
            return 0
        # Old portable exports can lack the original query. Two distinct,
        # non-overlapping fragments in a single label or note still give evidence.
        for label in [*labels,_clean(notes[:1200])]:
            terms=set(lexical(label,document=True)[0])
            if _two_fragments((pos,term) for pos,term in self.pairs if term in terms):
                return 1
        return 0


def _two_fragments(matches):
    first=next(matches,None)
    if first is None:
        return False
    start,term=first
    alternate=None
    for pos,other in matches:
        if other!=term:
            if pos>=start+2:
                return True
            if alternate is None:
                alternate=pos
        elif alternate is not None and pos>=alternate+2:
            return True
    return False


def _literal_url(value):
    try:
        url=urlsplit(value.rstrip('.,;:!?'))
    except ValueError:
        return None
    if url.scheme.lower() not in ('http','https') or not url.netloc:
        return None
    return url.scheme.lower(),url.netloc.lower(),url.path or '/',url.query


def _clean(text):
    return COMMON_HAN.sub(' ',URL.sub(' ',normal(text))).strip(' \t\r\n。！？!?,，')


def _han_anchor(term):
    return bool(re.fullmatch(r'[\u3400-\u9fff]+',term)
                and FUNCTION_HAN.isdisjoint(term))


def _anchors(text):
    return {t for t in lexical(text,'trigram',document=True)[0] if len(t)>=3 and (
        _han_anchor(t) or (t.isascii() and any(c.isalpha() for c in t) and t not in COMMON_ASCII))}
