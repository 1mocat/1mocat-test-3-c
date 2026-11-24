#!/usr/bin/env python3
"""Compressor that matches decomp.c.

Decoder recap:
  j = get_integer(9, 0)       # context base 0
  loop j times:
    bit = get_bit(1)
    if bit:
       offset = get_integer(5, 2) + 1    # context base 2*99=198
       length = get_integer(2, 3) + 1    # context base 3*99=297
       copy length bytes from Q-offset
    else:
       sign = get_bit(8)
       val  = get_integer(4, 9)         # context base 9*99=891
       *Q++ = (1-2*sign) * val          # stored as signed char
  printf("%s", buf)                      # relies on uninitialized stack being 0 after
"""
import sys

RADIX = 255
OFF1 = 5
OFF2 = 2
LITSIZE = 4

class RangeEnc:
    def __init__(self):
        self.low = 0
        self.high = 1
        self.counts = {}
        self.n_digits = 0

    def encode_bit(self, bit, ctx):
        while (self.high - self.low) < RADIX:
            self.low *= RADIX
            self.high *= RADIX
            self.n_digits += 1
        rng = self.high - self.low
        c = self.counts.get(ctx)
        if c is None:
            c = [0, 0]
            self.counts[ctx] = c
        split = rng * (c[0] + 1) // (c[0] + c[1] + 2)
        if bit == 0:
            self.high = self.low + split
        else:
            self.low = self.low + split
        c[bit] += 1

    def encode_int(self, n, T, ctx_outer):
        """Encode n >= 0 with parameter T; decoder's get_integer(T, ctx_outer)."""
        ctx_scaled = ctx_outer * 99
        val = n + (1 << T)
        bitlen = val.bit_length()
        k = bitlen - T  # number of unary steps (>=1)
        # k-1 zeros then one 1, at contexts ctx_scaled+T+1 .. ctx_scaled+T+k
        for i in range(1, k):
            self.encode_bit(0, ctx_scaled + T + i)
        self.encode_bit(1, ctx_scaled + T + k)
        # lower (T+k-1) bits of val, MSB first, at context ctx_scaled
        for i in range(T + k - 2, -1, -1):
            b = (val >> i) & 1
            self.encode_bit(b, ctx_scaled)

    def finish(self):
        v = self.low
        digits = []
        for _ in range(self.n_digits):
            digits.append(v % RADIX)
            v //= RADIX
        digits.reverse()
        return bytes(d + 1 for d in digits)


def find_longest_match(data, i, min_off=1):
    n = len(data)
    best_len = 0
    best_off = 0
    # offsets 1..i
    max_i = i
    max_len_possible = n - i
    for off in range(1, max_i + 1):
        # length lookup with overlap
        L = 0
        # quick fail
        if data[i - off] != data[i]:
            continue
        while L < max_len_possible and data[i + L - off] == data[i + L]:
            L += 1
        if L > best_len:
            best_len = L
            best_off = off
            # no early break — we want longest
    return best_off, best_len


def byte_to_lit(b):
    """Return (sign, val) to encode byte b."""
    if b <= 128:
        # option A: sign=0, val=b
        a = (0, b)
    else:
        a = None
    if b == 0:
        b_opt = (0, 0)
    else:
        b_opt = (1, (256 - b) & 0xFF)
    if a is None:
        return b_opt
    # Choose smaller val
    if a[1] <= b_opt[1]:
        return a
    return b_opt


def int_bits_cost(n, T):
    val = n + (1 << T)
    bitlen = val.bit_length()
    k = bitlen - T
    return (k - 1) + 1 + (T + k - 1)  # zeros + stop + raw

def lit_cost(b):
    sign, val = byte_to_lit(b)
    return 1 + 1 + int_bits_cost(val, LITSIZE)  # lit/match + sign + val

def match_cost(off, length):
    return 1 + int_bits_cost(off - 1, OFF1) + int_bits_cost(length - 1, OFF2)


def tokens_dp(data):
    """Dynamic programming over positions with static cost estimates."""
    n = len(data)
    INF = 10**18
    cost = [INF] * (n + 1)
    choice = [None] * (n + 1)  # (kind, off_or_byte, length)
    cost[0] = 0
    # literal cost cache
    for i in range(n):
        if cost[i] == INF:
            continue
        # literal
        b = data[i]
        c = cost[i] + lit_cost(b)
        if c < cost[i + 1]:
            cost[i + 1] = c
            choice[i + 1] = ('L', b, 1, i)
        # matches - find longest match at i, then try all lengths up to that
        if i == 0:
            continue
        # find all matches: for each offset, find max length
        # we approximate: scan all offsets, compute max length each
        # then for each length from 1 to max_len, use the smallest offset that reaches that length
        n_rem = n - i
        best_len_for_off = {}
        for off in range(1, i + 1):
            if data[i - off] != data[i]:
                continue
            L = 1
            while L < n_rem and data[i + L - off] == data[i + L]:
                L += 1
            if L >= 1:
                # record this (off, L): useful for lengths 1..L using offset `off`
                pass
            # For each prefix length l in 1..L, consider updating cost[i+l]
            # Use this offset only if better
            for l in range(1, L + 1):
                target = i + l
                if l == 1:
                    # length 1 match costs: 1 + off_cost + len_cost(0)
                    pass
                c_match = cost[i] + match_cost(off, l)
                if c_match < cost[target]:
                    cost[target] = c_match
                    choice[target] = ('M', off, l, i)
    # Reconstruct
    tokens = []
    pos = n
    while pos > 0:
        kind, a, b, prev = choice[pos]
        tokens.append((kind, a, b))
        pos = prev
    tokens.reverse()
    return tokens


def encode_tokens(tokens):
    enc = RangeEnc()
    enc.encode_int(len(tokens), 9, 0)
    for tok in tokens:
        if tok[0] == 'M':
            _, off, length = tok
            enc.encode_bit(1, 1)
            enc.encode_int(off - 1, OFF1, 2)
            enc.encode_int(length - 1, OFF2, 3)
        else:
            _, b, _ = tok
            enc.encode_bit(0, 1)
            sign, val = byte_to_lit(b)
            enc.encode_bit(sign, 8)
            enc.encode_int(val, LITSIZE, 9)
    return enc.finish()


def main():
    data = open(sys.argv[1], 'rb').read() if len(sys.argv) > 1 else sys.stdin.buffer.read()
    tokens = tokens_dp(data)
    out = encode_tokens(tokens)
    # emit to file
    outpath = sys.argv[2] if len(sys.argv) > 2 else 'data.comp'
    open(outpath, 'wb').write(out)
    # Stats
    print(f"input bytes: {len(data)}", file=sys.stderr)
    print(f"tokens: {len(tokens)}", file=sys.stderr)
    print(f"output bytes: {len(out)}", file=sys.stderr)


if __name__ == '__main__':
    main()
