#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Independent exact Fraction model, not a stock-algorithm instruction replay.

Reads frozen stock temperature profiles; never executes the C implementation.
Works directly with physical rational terminal voltages, not its scaled residuals.
"""
from fractions import Fraction as F
from pathlib import Path
import argparse
import json


def terminal(point, current, shunt, meter, dc):
    _, voltage, resistance = point
    return F(voltage) + F(current, 10000) * (F(resistance * dc, 100) + shunt + meter)


def at(a, b, fraction):
    return tuple(F(x) + fraction * (y - x) for x, y in zip(a, b))


def seed(points, voltage, parameters):
    loaded = [terminal(p, *parameters) for p in points]
    roots = {tuple(map(F, point)) for point, value in zip(points, loaded) if value == voltage}
    for a, b, v0, v1 in zip(points, points[1:], loaded, loaded[1:]):
        if a == b:
            continue
        if v0 == v1 == voltage:
            return -7, ()  # Nontrivial flat interval.
        if v0 != v1 and min(v0, v1) <= voltage <= max(v0, v1):
            if a[0] == b[0]:
                return -7, ()  # No unique charge-to-state model at this jump.
            roots.add(at(a, b, (voltage - v0) / (v1 - v0)))
    if len(roots) != 1:
        return (-6 if not roots else -7), ()
    return 0, tuple(x.numerator // x.denominator for x in roots.pop())


def cutoff(points, voltage, parameters):
    loaded = [terminal(p, *parameters) for p in points]
    if loaded[0] <= voltage:
        return -4, ()
    result, boundary = tuple(map(F, points[-1])), 2
    for a, b, v0, v1 in zip(points, points[1:], loaded, loaded[1:]):
        if v1 <= voltage:
            result = at(a, b, (voltage - v0) / (v1 - v0))
            boundary = 1 if a[0] == b[0] else 0
            break
    result = tuple(x.numerator // x.denominator for x in result)
    return (0, (*result, boundary)) if result[0] else (-4, ())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    profiles = json.loads((source / 'stock-oracles.json').read_text())['temperature_profiles']
    lines = []
    for case in profiles:
        points, temperature = case['profile'], case['temp_c']
        for dc in (80, 100, 125):
            for current in (-10000, -5000, -1, 0, 1, 5000, 10000):
                parameters = (current, 100, 75, dc)
                # Terminal coordinates are explicit inputs, plus exact/floored
                # row voltages and adjacent integers to exercise root changes.
                voltages = {25000, 30000, 33500, 36000, 39000, 42000, 45000, 50000}
                for point in points[::13]:
                    v = terminal(point, *parameters)
                    floor = v.numerator // v.denominator
                    voltages.update((floor, floor + 1))
                for voltage in sorted(voltages):
                    error, result = seed(points, voltage, parameters)
                    lines.append(' '.join(map(str, ('S', temperature, voltage, *parameters, error, *result))))
            for current in (-20000, -10000, -5000, 0):
                parameters = (current, 100, 75, dc)
                for voltage in (25000, 30000, 33500, 34000, 36000, 39000, 43000, 45000):
                    error, result = cutoff(points, voltage, parameters)
                    lines.append(' '.join(map(str, ('C', temperature, voltage, *parameters, error, *result))))
    args.out.write_text('\n'.join(lines) + '\n')
    print(f'Exact Fraction reference: {len(lines)} loaded-profile cases')


if __name__ == '__main__':
    main()
