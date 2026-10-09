#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Independent rational observation/session oracle, NOT stock daemon policy.

Uses frozen stock profile captures and the physical Fraction root/cutoff model.
All policy numbers below are synthetic test limits, never board calibration.
Normal-range arithmetic is unbounded Python/Fraction; C overflow rejection is
covered separately by hand-derived adversarial cases in session.c.
"""
from collections import Counter
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
import argparse
import importlib.util
import json

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('physical_model', HERE/'model-reference.py')
MODEL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODEL)
PROFILES = {c['temp_c']: c['profile'] for c in json.loads((HERE/'stock-oracles.json').read_text())['temperature_profiles']}
NAMES = ('minimum discharge shunt meter dc capture gap age expiry observed envelope '
         'v_motion i_motion t_motion model_error cutoff_error car_error q_width soc_width').split()
DEFAULT = dict(zip(NAMES, (33500, 5000, 100, 75, 100, 1000000000, 120000000000,
    600000000000, 60000000000, 1000000, 2000000, 20000, 300000, 9,
    5000, 3000, 200, 20000, 500)))
REASONS = 'INITIAL SEEDED TRACKED EVENT ORDER ACQUISITION ABSENT INPUT LIMIT TEMPERATURE COUNTER_AMBIGUOUS COUNTER_RATE MODEL DISAGREEMENT CUTOFF WIDTH ARITHMETIC GENERATION POLICY GAP EPOCH_AGE EXPIRED'.split()
R = {s: n for n, s in enumerate(REASONS)}


def floor(x): return x.numerator // x.denominator

def ceil(x): return -floor(-x)

def whole_c(x): return (1 if x >= 0 else -1) * (abs(x) // 10)

def raw_class(x): return (x >> 31, (x >> 11) & 0xfffff in (0, 0xfffff), (x >> 11) & 0xfffff == 0xfffff)

def hull_root(q, error): return (q*100-error, q*100+100+error)

def overlap(a, b):
    value = max(a[0], b[0]), min(a[1], b[1])
    return value if value[0] <= value[1] else None

def temporal(p, duration): return ceil(F(p['envelope'] * duration, 3600000000000))

def possible(p, a, b, duration): return abs(a-b) <= temporal(p, duration) + p['car_error']


class Reject(Exception):
    def __init__(self, why, detail=0): self.why, self.detail = why, detail


def require(condition, why, detail=0):
    if not condition: raise Reject(why, detail)


def observed(p, o):
    start, end, endpoints = o['start'], o['end'], o['ep']
    require(not o['error'], 'ACQUISITION', o['error'])
    for e in endpoints: require(not e['error'], 'ACQUISITION', e['error'])
    for e in endpoints:
        require(e['present'] != 0, 'ABSENT')
        raw = e['raw']; sign, special, _ = raw_class(raw)
        valid = (e['present'] == 1 and 0 <= raw <= 0xffffffff and e['v'] > 0 and
                 e['v'] % 1000 == e['i'] % 100 == e['car'] % 100 == 0 and
                 (not special or not e['car']) and
                 (e['car'] <= 0 if sign else e['car'] >= 0))
        require(valid, 'INPUT')
        require(-100 <= e['t'] <= 500, 'TEMPERATURE')
        require(abs(e['i']) <= p['observed'], 'LIMIT')
    a, b = endpoints
    temp = whole_c(a['t'])
    require(temp == whole_c(b['t']), 'TEMPERATURE')
    require(end-start <= p['capture'], 'LIMIT')
    for name in ('v', 'i', 't'): require(abs(a[name]-b[name]) <= p[name+'_motion'], 'LIMIT')
    require(raw_class(a['raw']) == raw_class(b['raw']), 'COUNTER_AMBIGUOUS')
    require(possible(p, a['car'], b['car'], end-start), 'COUNTER_RATE')
    roots = []
    for e in endpoints:
        error, root = MODEL.seed(PROFILES[temp], F(e['v'], 100),
                                 (F(e['i'], 100), p['shunt'], p['meter'], p['dc']))
        require(not error, 'MODEL', error)
        roots.append(hull_root(root[0], p['model_error'] + temporal(p, end-start)))
    q = overlap(*roots)
    require(q is not None, 'DISAGREEMENT', -6)
    error, cutoff = MODEL.cutoff(PROFILES[temp], p['minimum'],
                                 (-p['discharge'], p['shunt'], p['meter'], p['dc']))
    require(not error, 'CUTOFF', error)
    require(cutoff[3] != 2, 'CUTOFF', -4)
    usable = hull_root(cutoff[0], p['cutoff_error'])
    require(usable[0] > 0, 'CUTOFF', -4)
    return dict(q=q, usable=usable, temp=temp, boundary=cutoff[3])


def presentation(p, candidate):
    q, usable = candidate['q'], candidate['usable']
    require(q[1]-q[0] <= p['q_width'], 'WIDTH', -1)
    values = [10000*(1-F(qv, uv)) for qv in q for uv in usable]
    bounds = floor(min(values)), ceil(max(values))
    require(bounds[1]-bounds[0] <= p['soc_width'], 'WIDTH', -1)
    qmid, umid = (sum(q)//2, sum(usable)//2)
    point = max(0, min(10000, floor(10000*(1-F(qmid, umid)))))
    return (*q, *usable, *bounds, qmid, umid, point, candidate['boundary'])


class Session:
    def __init__(self):
        self.result = [0]*20
        self.active = False
        self.barrier_missing = False
        self.event_barrier = 0
        self.watermark = (0, 0, 0)
        self.seed = None
        self.last = None
        self.policy = None

    def apply(self, p, o):
        try:
            g, seq, end = self.watermark
            if o['event']:
                dated = o['gen'] > 0 and o['gen'] >= g and o['start'] > 0 and o['end'] >= o['start'] >= end
                self.barrier_missing = not dated
                require(dated, 'ORDER')
                self.watermark = o['gen'], seq if o['gen']==g else 0, o['end']
                self.event_barrier = o['end']
                raise Reject('EVENT')
            require(not self.barrier_missing and o['start'] > self.event_barrier and
                    o['gen'] > 0 and o['seq'] > 0 and o['start'] > 0 and o['end'] >= o['start'] and
                    o['gen'] >= g and o['start'] >= end and
                    (o['gen'] != g or o['seq'] > seq), 'ORDER')
            self.watermark = o['gen'], o['seq'], o['end']
            c = observed(p, o)
            reason = 'TRACKED'
            if not self.active: reason = 'SEEDED'
            elif o['gen'] != self.last['gen']: reason = 'GENERATION'
            elif p != self.policy: reason = 'POLICY'
            elif c['temp'] != self.result[9]: reason = 'TEMPERATURE'
            elif o['end'] - self.last['end'] > p['gap']: reason = 'GAP'
            elif o['end'] - self.seed['end'] > p['age']: reason = 'EPOCH_AGE'
            elif raw_class(o['ep'][0]['raw']) != raw_class(self.last['ep'][1]['raw']): reason = 'COUNTER_AMBIGUOUS'
            if reason == 'TRACKED':
                car = o['ep'][1]['car']
                require(possible(p, car, self.last['ep'][1]['car'], o['end']-self.last['end']), 'COUNTER_RATE')
                require(possible(p, car, self.seed['ep'][1]['car'], o['end']-self.seed['end']), 'COUNTER_RATE')
                change = car-self.seed['ep'][1]['car']
                prediction = (self.seed_q[0]-change-p['car_error'], self.seed_q[1]-change+p['car_error'])
                c['q'] = overlap(c['q'], prediction)
                require(c['q'] is not None, 'DISAGREEMENT')
                c['usable'] = tuple(self.result[12:14])
                epoch = self.result[4]
            else:
                epoch = self.result[4]+1
            fields = presentation(p, c)
            if reason != 'TRACKED':
                self.seed, self.seed_q = deepcopy(o), c['q']
            self.result = [1, R[reason], 0, 0, epoch, o['gen'], o['seq'], o['end'],
                           min(o['end']+p['expiry'], self.seed['end']+p['age']), c['temp'], *fields]
            self.last, self.policy = deepcopy(o), deepcopy(p)
            self.active = True
        except Reject as failure:
            self.result[0] = 2 if self.result[4] else 0
            self.result[1:4] = R[failure.why], failure.detail, o['event']
            self.active = False
        return self.result[:]

    def read(self, now):
        result = self.result[:]
        if result[0] == 1:
            if not result[7] <= now <= result[8]:
                result[0:2] = 2, R['ORDER' if now < result[7] else 'EXPIRED']
            else:
                movement = temporal(self.policy, now-result[7])
                candidate = dict(q=(result[10]-movement,result[11]+movement),
                                 usable=result[12:14],boundary=result[19])
                try:
                    result[10:] = presentation(self.policy,candidate)
                except Reject as failure:
                    result[0:3] = 2,R[failure.why],failure.detail
        return result


def counter(magnitude, negative=False):
    """Generate normal counter inputs in the verified unity-gain/shunt units.
    Session does not implement this conversion; dedicated 0069 tests pin it.
    """
    converted = ((magnitude*11176//10000 + 5)//10)*100
    raw = ((0xfffff-magnitude if negative else magnitude) << 11) | (int(negative) << 31)
    return raw, -converted if negative else converted


def voltage(temp, q_uah, current):
    points = PROFILES[whole_c(temp)]
    q = F(q_uah, 100)
    for a, b in zip(points, points[1:]):
        if a[0] <= q <= b[0] and a[0] != b[0]:
            point = MODEL.at(a, b, (q-a[0])/(b[0]-a[0]))
            v = MODEL.terminal(point, F(current, 100), 100, 75, 100)
            return floor(v/10)*1000  # Actual ADC integer-mV output domain.
    raise ValueError(q)


def observation(seq=1, temp=250, current=-100000, q=400000, magnitude=10000, negative=False, gen=1, start=None):
    raw, car = counter(magnitude, negative)
    e = dict(error=0, present=1, raw=raw, car=car, i=current, v=voltage(temp, q, current), t=temp)
    start = seq*10000000000 if start is None else start
    return dict(gen=gen, seq=seq, start=start, end=start+100000000, error=0, event=0, ep=[deepcopy(e), deepcopy(e)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    lines, counts = [], Counter()
    session, policy = Session(), deepcopy(DEFAULT)
    def reset(p=None):
        nonlocal session, policy
        session, policy = Session(), deepcopy(DEFAULT if p is None else p)
        lines.extend(('Z', 'P '+' '.join(str(policy[k]) for k in NAMES)))
    def step(o):
        expected = session.apply(policy, o)
        counts[REASONS[expected[1]]] += 1
        vals = [o[k] for k in ('gen','seq','start','end','error','event')]
        vals += [e[k] for e in o['ep'] for k in ('error','present','raw','car','i','v','t')]
        lines.append('O '+' '.join(map(str, vals+expected)))
        return expected
    def read(now):
        counts['READ'] += 1
        lines.append('R '+' '.join(map(str, [now]+session.read(now))))
    # Grid includes both CAR signs, charging/discharging/zero/near-zero current,
    # cold/interpolated/hot profiles and recovery after real invalid roots.
    for temp in (-100, -50, 0, 90, 250, 370, 500):
        for current in (-500000, -100000, -100, 0, 100, 100000, 500000):
            for q in (10000, 150000, 400000, 750000, 900000):
                for negative in (False, True):
                    reset()
                    first = observation(temp=temp, current=current, q=q, negative=negative)
                    baseline = first['ep'][1]['car']
                    step(first)
                    for seq, mag in ((2,10009),(3,9991),(4,10018)):
                        _, car = counter(mag, negative)
                        step(observation(seq, temp, current, q-(car-baseline), mag, negative))
                    read(max(1, session.result[8])); read(session.result[8]+1)
                    if session.result[7]: read(session.result[7]-1)
    # Every semantic error/reseed branch, at both initial and previously good state.
    mutations = []
    def mutate(name, fn): mutations.append((name, fn))
    mutate('error', lambda o: o.update(error=-5))
    for index in (0,1):
        mutate('endpoint-error', lambda o, i=index: o['ep'][i].update(error=-11))
        mutate('absence', lambda o, i=index: o['ep'][i].update(present=0))
        mutate('bad-present', lambda o, i=index: o['ep'][i].update(present=2))
        for name,value in (('raw',-1),('raw',0x100000000),('v',0),('v',3900001),('i',1),('car',1),('t',501),('t',-101),('i',1000100)):
            mutate('invalid-field', lambda o, i=index,k=name,v=value: o['ep'][i].update({k:v}))
    mutate('time', lambda o: o.update(start=0))
    mutate('time', lambda o: o.update(end=o['start']-1))
    mutate('duration', lambda o: o.update(end=o['start']+DEFAULT['capture']+1))
    mutate('temp-profile', lambda o: o['ep'][1].update(t=260))
    mutate('motion-v', lambda o: o['ep'][1].update(v=o['ep'][0]['v']+21000))
    mutate('motion-i', lambda o: o['ep'][1].update(i=300000))
    mutate('raw-sign', lambda o: o['ep'][1].update(raw=counter(10000,True)[0],car=-o['ep'][0]['car']))
    mutate('raw-zero', lambda o: o['ep'][1].update(raw=0,car=0))
    mutate('raw-allone', lambda o: o['ep'][1].update(raw=0xfffff<<11,car=0))
    mutate('counter-rate', lambda o: o['ep'][1].update(car=o['ep'][0]['car']+1000))
    mutate('no-root', lambda o: [e.update(v=5000000) for e in o['ep']])
    mutate('raw-special-converted', lambda o: [e.update(raw=0) for e in o['ep']])
    for initial in (True,False):
        for _, change in mutations:
            reset()
            if not initial: step(observation())
            o=observation(2); change(o); step(o)
            step(observation(3)); read(30000000001)
    for event in (1,2,3,4):
        reset(); step(observation())
        o=observation(2); o['event']=event; step(o); step(observation(3))
    for cause in ('GENERATION','POLICY','TEMPERATURE','GAP','EPOCH_AGE','COUNTER_AMBIGUOUS','ORDER','COUNTER_RATE','DISAGREEMENT'):
        reset(); step(observation())
        o=observation(2)
        if cause=='GENERATION': o.update(gen=2,seq=1)
        elif cause=='POLICY':
            policy['model_error']+=1
            lines.append('P '+' '.join(str(policy[k]) for k in NAMES))
        elif cause=='TEMPERATURE': o=observation(2,temp=260)
        elif cause=='GAP': o=observation(2,start=200000000000)
        elif cause=='EPOCH_AGE':
            for seq in range(2,9): step(observation(seq,start=seq*100000000000))
            o=observation(9,start=900000000000)
        elif cause=='COUNTER_AMBIGUOUS': o=observation(2,negative=True)
        elif cause=='ORDER': o=observation()
        elif cause=='COUNTER_RATE':
            for e in o['ep']: e['car']+=100000
        elif cause=='DISAGREEMENT': o=observation(2,q=600000)
        expected=step(o)
        assert expected[1]==R[cause], (cause,expected)
    # Strict event causality differs from inclusive ordinary adjacency. An
    # in-flight capture starting at the event's clock value can finish later
    # without proving that it started after the invalidation.
    reset(); step(observation())
    event=observation(2,start=30000000000); event.update(event=1,end=30000000000,seq=0)
    step(event)
    for seq,start,end in ((2,29999999999,29999999999),
                          (3,30000000000,30000000000),
                          (4,30000000000,30000000001),
                          (5,30000000001,30000000001),
                          (6,30000000001,30000000001)):
        o=observation(seq,start=start); o['end']=end; result=step(o)
        assert result[1]==R['ORDER' if seq<5 else 'SEEDED' if seq==5 else 'TRACKED']
    # Accepted stable special codes, ambiguity across their transitions, low
    # fractional raw bits ignored for class, no invented modulo correction.
    for raw in (0,0x7ff,0xfffff<<11,0x80000000,0xfffff800,0xffffffff):
        reset()
        for seq in (1,2):
            o=observation(seq)
            for e in o['ep']: e.update(raw=raw,car=0)
            step(o)
    # Explicit cutoff/uncertainty policy boundaries, no hidden capacity fallback.
    for key,value in (('minimum',25000),('minimum',50000),('cutoff_error',2000000),
                      ('model_error',0),('q_width',1),('soc_width',1),('cutoff_error',0)):
        p=deepcopy(DEFAULT); p[key]=value; reset(p); step(observation())
    args.out.write_text('\n'.join(lines)+'\n')
    (args.out.parent/'session-reference-counts.json').write_text(json.dumps(dict(counts),indent=2)+'\n')
    assert counts['SEEDED']>300 and counts['TRACKED']>900
    print(f'Rational session reference: {sum(counts.values())} operations; {dict(counts)}')


if __name__=='__main__': main()
