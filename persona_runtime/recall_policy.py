"""PCAR recall policy adapter. No IO and no model calls."""
from dataclasses import replace
from math import isfinite
from .vendor.amemorix_lifecycle import (
    RelationLifecyclePolicy,RelationLifecycleState,RelationLifecycleEvent,
    retention_at,threshold_crossing_at,apply_lifecycle_event)

VERSION='recall-support-v1'
KINDS=('memory','dialogue','native')
HALF_LIFE_DAYS={'memory':90,'dialogue':30,'native':14}
ALPHAS={'context_adopted':0.12,'user_mention':0.18,'supported_recall':0.24}
INITIAL_PRIORITY=0.65
ACTIVE_THRESHOLD=0.2
MAX_CLOCK_JUMP=30*86400
MAX_REPLAY_EVENTS=5000

def finite(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not isfinite(value):
        raise ValueError('recall_nonfinite_value')
    return float(value)

def policy(kind):
    if kind not in KINDS:raise ValueError('recall_kind_invalid')
    return RelationLifecyclePolicy(half_life_hours=HALF_LIFE_DAYS[kind]*24,
        freeze_threshold=ACTIVE_THRESHOLD,revive_threshold=ACTIVE_THRESHOLD+0.01,
        access_cooldown_seconds=0)

def priority(kind,value,anchor,now):
    return retention_at(RelationLifecycleState(finite(value),finite(anchor)),now=finite(now),policy=policy(kind))

def due(kind,value,anchor):
    return threshold_crossing_at(strength=finite(value),anchor_at=finite(anchor),policy=policy(kind))

def reinforce(kind,value,anchor,now,alpha):
    # Every independent event contributes; upstream's access cooldown is not used.
    p=replace(policy(kind),reinforce_alpha=finite(alpha))
    result=apply_lifecycle_event(RelationLifecycleState(value,anchor),RelationLifecycleEvent.REINFORCE,
        now=now,policy=p)
    return result.strength,result.anchor_at

def clock(previous,observed,*,confirm_jump=False):
    observed=finite(observed)
    if observed<0:raise ValueError('recall_time_invalid')
    if previous is None:return observed,None
    if observed<previous:return previous,'clock_rollback'
    if observed-previous>MAX_CLOCK_JUMP and not confirm_jump:return previous,'clock_forward_requires_confirmation'
    return observed,None

def snapshot():
    return {'version':VERSION,'half_life_days':HALF_LIFE_DAYS,'initial_priority':INITIAL_PRIORITY,
        'active_threshold':ACTIVE_THRESHOLD,'event_alphas':ALPHAS,'max_clock_jump_seconds':MAX_CLOCK_JUMP,
        'max_replay_events':MAX_REPLAY_EVENTS,'initialization':'registration_anchor_unknown_historical_usage',
        'ranking':'relevance_then_priority','independent_events':'positive_no_access_cooldown'}
