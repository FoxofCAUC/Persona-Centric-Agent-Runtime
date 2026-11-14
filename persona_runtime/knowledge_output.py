"""Historical data date codec only. The retired extraction/verdict prompts are gone."""
from datetime import datetime,timezone
from .models import RuntimeErrorBase
VERSION='retired-public-knowledge-codec'
MAIN_VERSION=VERSION
RELATIVE_TIME=r'今天|明天|昨天'
def date(value):
    if value is None:return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result.astimezone(timezone.utc).isoformat() if result.tzinfo else value
    except (ValueError,TypeError,AttributeError):raise RuntimeErrorBase('knowledge_transfer_invalid') from None
def messages(payload):raise RuntimeErrorBase('legacy_retrieval_frozen')
