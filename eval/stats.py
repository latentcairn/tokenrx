# -*- coding: utf-8 -*-
"""표준 라이브러리만 쓴 통계: Fisher 정확 검정, McNemar 정확 검정, 부트스트랩 신뢰구간."""
import math
import random


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def fisher_exact(k1, n1, k2, n2):
    """두 집단의 성공 비율이 같은지(양측). k: 성공 수, n: 시행 수. 2x2 표 [[k1, n1-k1], [k2, n2-k2]]."""
    if not (0 <= k1 <= n1 and 0 <= k2 <= n2):
        raise ValueError("counts out of range")
    k, n = k1 + k2, n1 + n2
    if n == 0:
        return 1.0
    denom = math.comb(n, k)

    def p(x):   # 여백 합을 고정했을 때 첫 칸이 x일 확률(초기하 분포)
        return math.comb(n1, x) * math.comb(n2, k - x) / denom

    observed = p(k1)
    lo, hi = max(0, k - n2), min(k, n1)
    total = sum(p(x) for x in range(lo, hi + 1) if p(x) <= observed * (1 + 1e-7))
    return min(1.0, total)


def mcnemar_exact(b, c):
    """짝지은 두 결과가 같은지(양측). b: A만 성공, c: B만 성공한 짝의 수."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _percentile(sorted_xs, q):
    if not sorted_xs:
        return 0.0
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def bootstrap_diff(a, b, paired=False, n_boot=10_000, alpha=0.05, seed=0):
    """평균 차이(b - a)와 백분위 부트스트랩 신뢰구간 (점추정, 하한, 상한).

    paired=True면 a[i]와 b[i]를 한 쌍으로 보고 차이를 다시 뽑는다(같은 문제로 두 방식을 돌린 경우).
    """
    a, b = list(a), list(b)
    if not a or not b:
        return (0.0, 0.0, 0.0)
    rng = random.Random(seed)
    if paired:
        if len(a) != len(b):
            raise ValueError("paired samples must have the same length")
        d = [y - x for x, y in zip(a, b)]
        point = mean(d)
        boots = sorted(mean(rng.choice(d) for _ in d) for _ in range(n_boot))
    else:
        point = mean(b) - mean(a)
        boots = sorted(mean(rng.choice(b) for _ in b) - mean(rng.choice(a) for _ in a) for _ in range(n_boot))
    return (point, _percentile(boots, alpha / 2), _percentile(boots, 1 - alpha / 2))
