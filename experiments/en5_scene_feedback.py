"""Bind the blind feedback to the 16 author-selected scene results."""
from collections import Counter

from .corpus_reply_study import read_private, save_once
from .en5_decision_packet import file_hash
from .en5_scene_compare import STUDY
from .en5_scene_voice import require

GRADES = {'accept','service','help','reject','uncertain'}


def import_feedback(revision, submitted):
    folder = revision/'manual-pro'
    result = read_private(folder/'results.private.json')
    key = read_private(folder/'blind-key.private.json')
    review = read_private(submitted)
    expected = {'version':'en5-style-feedback-v1','study_id':STUDY,
                'input_contract_sha256':file_hash(revision/'requests.private.json'),
                'results_sha256':file_hash(folder/'results.private.json'),
                'blind_key_sha256':file_hash(folder/'blind-key.private.json')}
    require(all(review.get(k)==value for k,value in expected.items())
            and key['binding']==result['binding']
            and set(review['positions'])==set(key['labels'])==set(result['results'])
            and len(review['positions'])==16 and isinstance(review['overall'],str),'feedback_scope')
    ratings = {'A':Counter(),'B':Counter()}
    preferred = Counter()
    both_rejected = []
    decoded = {}
    for pos, row in review['positions'].items():
        labels = key['labels'][pos]
        grades = row['ratings']
        ranks = row['ranks']
        require(set(labels)==set(grades)==set('甲乙') and set(labels.values())=={'A','B'}
                and set(grades.values())<=GRADES and set(ranks)<=set(labels)
                and set(ranks.values())<={'1','2'} and isinstance(row['note'],str),'feedback_position')
        rejected = all(g in {'service','help','reject'} for g in grades.values())
        require(row['all_unacceptable'] is rejected and (not rejected or not ranks),'feedback_grade')
        if rejected:
            both_rejected.append(pos)
        for label, grade in grades.items():
            ratings[labels[label]][grade]+=1
        order = {labels[label]:int(rank) for label,rank in ranks.items()}
        if len(order)==2:
            preferred['tie' if order['A']==order['B'] else min(order,key=order.__getitem__)]+=1
        decoded[pos] = {'grades':{labels[k]:v for k,v in grades.items()},'ranks':order,
                        'both_rejected':rejected,'note':row['note']}
    report = {'version':'en5-scene-manual-pro-author-review-v1','submitted_sha256':file_hash(submitted),
              'review_binding':expected,'grades':{k:dict(v) for k,v in ratings.items()},
              'preferred':dict(preferred),'both_rejected':both_rejected,'overview':review['overall'],'positions':decoded}
    archive = folder/'author-feedback'/report['submitted_sha256']
    save_once(archive/'submitted.private.json',review)
    checksum = save_once(archive/'decoded.private.json',report)
    return archive, checksum, report
