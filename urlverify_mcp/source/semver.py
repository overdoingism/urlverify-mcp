"""npm-style semver ranges (node-semver semantics for the common grammar). Anything outside the grammar raises
ValueError so the caller reports VERSION_RANGE_UNSUPPORTED instead of guessing."""
from __future__ import annotations

import re

_V = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_P = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(v: str):
    m = _V.match(v.strip())
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3]), tuple(m[4].split(".")) if m[4] else ()


def _pre_key(pre: tuple) -> tuple:
    if not pre:
        return (1,)                                   # a release sorts after its prereleases
    return (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)


def key(v) -> tuple:
    return v[0], v[1], v[2], _pre_key(v[3])


def _partial(s: str):
    m = _P.match(s.strip())
    if not m:
        raise ValueError(f"not a version: {s!r}")
    parts = [None if (x is None or x in "xX*") else int(x) for x in m.group(1, 2, 3)]
    # anything after a wildcard is a wildcard too
    for i in range(3):
        if parts[i] is None:
            parts[i + 1:] = [None] * (2 - i)
            break
    pre = tuple(m[4].split(".")) if m[4] else ()
    return parts, pre


def _full(parts, pre=()):
    return (parts[0] or 0, parts[1] or 0, parts[2] or 0, pre)


def _comparators(expr: str) -> list[tuple[str, tuple]]:
    """One space-separated comparator set -> primitive comparators [(op, version)]."""
    e = expr.strip()
    if e in ("", "*", "x", "X", "latest"):
        return []
    m = re.fullmatch(r"(\S+)\s+-\s+(\S+)", e)
    if m:
        lo, lop = _partial(m[1])
        hi, hip = _partial(m[2])
        out = [(">=", _full(lo, lop))]
        if hi[0] is None:
            return out
        if hi[1] is None:
            out.append(("<", (hi[0] + 1, 0, 0, ("0",))))
        elif hi[2] is None:
            out.append(("<", (hi[0], hi[1] + 1, 0, ("0",))))
        else:
            out.append(("<=", _full(hi, hip)))
        return out
    out: list[tuple[str, tuple]] = []
    for tok in re.sub(r"(>=|<=|>|<|=|\^|~>|~)\s+", r"\1", e).split():
        mm = re.fullmatch(r"(>=|<=|>|<|=|\^|~>|~)?(.+)", tok)
        op, ver = mm[1] or "", mm[2]
        p, pre = _partial(ver)
        M, m_, pa = p
        if op in ("", "="):
            if M is None:
                continue
            if m_ is None:
                out += [(">=", (M, 0, 0, ())), ("<", (M + 1, 0, 0, ("0",)))]
            elif pa is None:
                out += [(">=", (M, m_, 0, ())), ("<", (M, m_ + 1, 0, ("0",)))]
            else:
                out.append(("=", (M, m_, pa, pre)))
        elif op in ("~", "~>"):
            if M is None:
                continue
            out.append((">=", _full(p, pre)))
            out.append(("<", (M, (m_ or 0) + 1, 0, ("0",)) if m_ is not None else (M + 1, 0, 0, ("0",))))
        elif op == "^":
            if M is None:
                continue
            out.append((">=", _full(p, pre)))
            if M > 0 or m_ is None:
                out.append(("<", (M + 1, 0, 0, ("0",))))
            elif m_ > 0 or pa is None:
                out.append(("<", (0, m_ + 1, 0, ("0",))))
            else:
                out.append(("<", (0, 0, pa + 1, ("0",))))
        else:
            if M is None:
                if op in ("<", ">"):
                    out.append(("<", (0, 0, 0, ("0",))))       # matches nothing
                continue
            if op == ">":
                if m_ is None:
                    out.append((">=", (M + 1, 0, 0, ())))
                elif pa is None:
                    out.append((">=", (M, m_ + 1, 0, ())))
                else:
                    out.append((">", (M, m_, pa, pre)))
            elif op == "<=":
                if m_ is None:
                    out.append(("<", (M + 1, 0, 0, ("0",))))
                elif pa is None:
                    out.append(("<", (M, m_ + 1, 0, ("0",))))
                else:
                    out.append(("<=", (M, m_, pa, pre)))
            elif op == ">=":
                out.append((">=", _full(p, pre)))
            elif op == "<":
                out.append(("<", _full(p, pre) if pa is not None or pre else (M, m_ or 0, 0, ("0",)) if m_ is not None else (M, 0, 0, ("0",))))
    return out


def _satisfies(v, comps, include_prerelease: bool) -> bool:
    kv = key(v)
    for op, c in comps:
        kc = key(c)
        ok = {">=": kv >= kc, ">": kv > kc, "<=": kv <= kc, "<": kv < kc, "=": kv == kc}[op]
        if not ok:
            return False
    if v[3] and not include_prerelease:
        # node-semver: a prerelease only matches when some comparator names a prerelease of the same M.m.p
        return any(c[3] and c[3] != ("0",) and c[:3] == v[:3] for _, c in comps)
    return True


def max_satisfying(versions, range_expr: str, include_prerelease: bool = False) -> str | None:
    alts = [_comparators(a) for a in range_expr.split("||")]
    best, best_key = None, None
    for s in versions:
        v = parse_version(s)
        if v is None:
            continue
        if any(_satisfies(v, comps, include_prerelease) for comps in alts):
            if best_key is None or key(v) > best_key:
                best, best_key = s, key(v)
    return best
