// morton.js の純関数ユニットテスト（E10-9 / Issue #66）。
//   node --test tests/viewer
import test from "node:test";
import assert from "node:assert/strict";

import {
  MORTON_FAST_PATH_MAX_CODE,
  decodeMortonIndices,
  mortonDecode,
} from "../../ifc2usd/viewer/lib/morton.js";

test("decodes small codes", () => {
  assert.deepEqual(mortonDecode(0), [0, 0, 0]);
  assert.deepEqual(mortonDecode(1), [1, 0, 0]);
  assert.deepEqual(mortonDecode(2), [0, 1, 0]);
  assert.deepEqual(mortonDecode(4), [0, 0, 1]);
  assert.deepEqual(mortonDecode(7), [1, 1, 1]);
});

test("decodes the largest fast-path code", () => {
  // 30bit すべて 1 = 各軸 10bit すべて 1
  assert.equal(MORTON_FAST_PATH_MAX_CODE, 0x3fffffff);
  assert.deepEqual(mortonDecode(MORTON_FAST_PATH_MAX_CODE), [1023, 1023, 1023]);
});

test("crosses into the BigInt path without wrapping", () => {
  // bit30 = 3*10 + 0 -> x の 10bit 目だけが立つ。JS の << は 32bit で
  // シフト量が mod 32 に丸められるため、素朴な実装だとここが壊れる。
  assert.deepEqual(mortonDecode(0x40000000), [1024, 0, 0]);
});

test("decodes 63-bit codes (spec.md §2 の 21bit/軸)", () => {
  // bit62 = 3*20 + 2 -> z の 20bit 目
  assert.deepEqual(mortonDecode(2 ** 62), [0, 0, 1 << 20]);
});

test("round-trips a hand-encoded code", () => {
  const encode = (x, y, z) => {
    let code = 0n;
    for (let i = 0n; i < 21n; i++) {
      code |= ((BigInt(x) >> i) & 1n) << (3n * i);
      code |= ((BigInt(y) >> i) & 1n) << (3n * i + 1n);
      code |= ((BigInt(z) >> i) & 1n) << (3n * i + 2n);
    }
    return Number(code);
  };
  for (const [x, y, z] of [[3, 5, 9], [1023, 0, 2047], [100000, 3, 7]]) {
    assert.deepEqual(mortonDecode(encode(x, y, z)), [x, y, z]);
  }
});

test("passes plain index arrays through unchanged (v2 互換)", () => {
  assert.deepEqual(decodeMortonIndices([5, 7, 9]), [5, 7, 9]);
});

test("expands delta+RLE encoded indices", () => {
  assert.deepEqual(
    decodeMortonIndices({ base: 10, deltas: [[1, 3], [5, 2]] }),
    [10, 11, 12, 13, 18, 23],
  );
});

test("treats a missing base as empty", () => {
  assert.deepEqual(decodeMortonIndices({ base: null, deltas: [] }), []);
  assert.deepEqual(decodeMortonIndices({}), []);
});
