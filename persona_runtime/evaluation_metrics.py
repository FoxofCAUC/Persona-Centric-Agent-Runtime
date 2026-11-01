"""Frozen count gates for release; missing evidence never shrinks denominators."""
import math


def wilson(wins):
    z=1.96;n=100;p=wins/n
    return (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def quality_gates(counts):
    required={'situation_top1':(0,40),'situation_top2':(0,40),'implicit_gain':(-40,40),
        'reasonable_pairs':(0,20),'unnecessary_cases':(0,20),'action_implementation':(0,100),
        'natural_wins':(0,100),'b_task_success':(0,100),'c_task_success':(0,100),'critical_errors':(0,100)}
    if type(counts) is not dict or set(counts)!=set(required):raise ValueError('evaluation_metric_contract')
    for k,(lo,hi) in required.items():
        if type(counts[k]) is not int or not lo<=counts[k]<=hi:raise ValueError('evaluation_metric_denominator')
    c=counts
    return {'situation':c['situation_top1']>=34 and c['situation_top2']>=38,
        'implicit':c['implicit_gain']>=6,'pairs':c['reasonable_pairs']>=16,
        'control':c['unnecessary_cases']<=2,'implementation':c['action_implementation']>=95,
        'naturalness':c['natural_wins']>=60 and wilson(c['natural_wins'])>.5,
        'task':c['c_task_success']>=c['b_task_success'] and c['critical_errors']==0}
