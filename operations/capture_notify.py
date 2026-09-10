#!/usr/bin/env python3
"""One bounded Pushover notification to the operator; never retry ambiguous sends."""
import json
import logging
from pathlib import Path
import shlex
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def notify(credentials, *, service='council-capture-evidence.service',
           evidence_root='/var/lib/ai-council-evidence/live-capture-20260907',
           open_url=urlopen):
    values={}
    for line in Path(credentials).read_text().splitlines():
        line=line.strip().removeprefix('export ')
        if '=' not in line or line.startswith('#'):
            continue
        key,value=line.split('=',1)
        if key in ('PUSHOVER_USER_KEY','PUSHOVER_TOKEN'):
            parsed=shlex.split(value,comments=True)
            if len(parsed)!=1:
                raise ValueError('invalid notification credential configuration')
            values[key]=parsed[0]
    if set(values)!= {'PUSHOVER_USER_KEY','PUSHOVER_TOKEN'}:
        raise ValueError('notification credentials missing')
    data=urlencode({'token':values['PUSHOVER_TOKEN'],'user':values['PUSHOVER_USER_KEY'],
        'title':'Council capture maintenance failed','priority':0,
        'message':f'Evidence renewal on manny failed. Retained cycles require inspection; do not retry an ambiguous upload. Check {service} and {evidence_root}. Capture may become unhealthy.'}).encode()
    with open_url(Request('https://api.pushover.net/1/messages.json',data=data,method='POST'),timeout=15) as response:
        result=json.loads(response.read(16384))
    if result.get('status')!=1:
        raise ValueError('notification was not accepted')
    logging.info('Council capture failure notification accepted')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--service',default='council-capture-evidence.service')
    parser.add_argument('--evidence-root',default='/var/lib/ai-council-evidence/live-capture-20260907')
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    notify('/home/trader/.config/plaintape/pushover.env',service=args.service,
           evidence_root=args.evidence_root)
