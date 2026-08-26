/**
 * Morton (Z-order) コードの復号。`ifc2usd/voxel.py` の morton_encode/decode と対。
 *
 * three.js/DOM に依存しない純関数なので、Playwright を通さず `node --test` で
 * 直接検証できる（E10-9 / Issue #66）。
 */

// spec.md §2 は3軸21bitまで(=最大63bit)のMortonコードを許容するが、JSのビット
// 演算子(<<, |, &)は32bit符号付き整数に丸められる。さらにシフト量が32以上になると
// 0を返さずラップアラウンドしてしまう（コード自体が32bitに収まるかどうかとは別の
// 制約）。ループは `code >> (3*i)` が0になるまで回るため、コードの最上位ビット位置L
// に対し最終的に評価するシフト量は 3*ceil(L/3) になる。これが31以下に収まる最大の
// Lは30（ceil(30/3)*3=30）なので、閾値は2^30-1に取る（2^31-1まで許すと31bit境界で
// シフト量が33になりラップアラウンドして壊れる）。
export const MORTON_FAST_PATH_MAX_CODE = 0x3fffffff;

export function mortonDecode(code) {
  if (code <= MORTON_FAST_PATH_MAX_CODE) {
    // 大半のコード(10bit/軸強まで)は普通のNumberでのビット演算で十分正確かつ高速。
    let x = 0;
    let y = 0;
    let z = 0;
    let i = 0;
    while (code >> (3 * i) > 0) {
      x |= ((code >> (3 * i)) & 1) << i;
      y |= ((code >> (3 * i + 1)) & 1) << i;
      z |= ((code >> (3 * i + 2)) & 1) << i;
      i += 1;
    }
    return [x, y, z];
  }

  // fast path を超えたまれなケースのみ、より遅いBigIntで63bit全域を復元する。
  let c = BigInt(code);
  let x = 0n;
  let y = 0n;
  let z = 0n;
  let i = 0n;
  while (c >> (3n * i) > 0n) {
    x |= ((c >> (3n * i)) & 1n) << i;
    y |= ((c >> (3n * i + 1n)) & 1n) << i;
    z |= ((c >> (3n * i + 2n)) & 1n) << i;
    i += 1n;
  }
  return [Number(x), Number(y), Number(z)];
}

// voxels.json v3の`indices`はdelta+RLE符号化された{base, deltas}形式（Issue #38 /
// E7-4、ifc2usd.voxel.encode_morton_indicesと対）。素朴な配列（v2互換ファイルや
// convertV1VoxelJsonの出力）はそのまま返し、両形式を透過的に扱う。
export function decodeMortonIndices(indices) {
  if (Array.isArray(indices)) return indices;
  const codes = [];
  if (indices.base === null || indices.base === undefined) return codes;
  codes.push(indices.base);
  for (const [delta, count] of indices.deltas ?? []) {
    for (let i = 0; i < count; i++) {
      codes.push(codes[codes.length - 1] + delta);
    }
  }
  return codes;
}
