"""General retrieval context only; source-audit serialization is retired."""
def attach(messages,details,result,turn):
    if (result or {}).get('mode')=='conversational_web_v1':
        from .knowledge_general import attach as general_attach
        return general_attach(messages,details,result,turn)
    return messages,details
