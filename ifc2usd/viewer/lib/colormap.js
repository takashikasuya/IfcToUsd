/**
 * turbo カラーマップ（E9-4 の値→色マッピング）。three.js/DOM に依存しない純関数。
 *
 * turbo系カラーマップの多項式近似 (Anton Mikhailov / Google Research, 2019,
 * Apache-2.0 "Turbo, An Improved Rainbow Colormap for Visualization" を移植)。
 * 外部ファイル・CDN依存なしで256エントリのLUTを事前計算する（build_twin_json()の
 * colormap名 "turbo" に対応する唯一の実装、E9-4の受け入れ条件）。
 */

export function turboColor(x) {
  x = Math.min(1, Math.max(0, x));
  const x2 = x * x;
  const x3 = x2 * x;
  const x4 = x2 * x2;
  const x5 = x3 * x2;
  const r =
    0.13572138 + 4.6153926 * x - 42.66032258 * x2 + 132.13108234 * x3 - 152.94239396 * x4 + 59.28637943 * x5;
  const g =
    0.09140261 + 2.19418839 * x + 4.84296658 * x2 - 14.18503333 * x3 + 4.27729857 * x4 + 2.82956604 * x5;
  const b =
    0.1066733 + 12.64194608 * x - 60.58204836 * x2 + 110.36276771 * x3 - 89.90310912 * x4 + 27.34824973 * x5;
  return [Math.min(1, Math.max(0, r)), Math.min(1, Math.max(0, g)), Math.min(1, Math.max(0, b))];
}

export const TURBO_LUT = Array.from({ length: 256 }, (_, i) => turboColor(i / 255));

// 凡例のグラデーションバーはCSS linear-gradientで描く（<canvas>にしない）。
// <canvas>にすると、既存の全PlaywrightテストがWebGL描画結果の画素検証に使う
// `document.querySelector('#viewport canvas')`（唯一のcanvas要素という前提）が、
// レンダラーのcanvas(viewport.appendChild(renderer.domElement)でJS実行時に
// 追加され、DOM順で常にこの凡例より後になる)より先にこちらへマッチしてしまい、
// 3D描画ではなく凡例バーの画素を検証してしまう（実際に踏んだ回帰）。
export const TURBO_GRADIENT_CSS = (() => {
  const stops = 16;
  const parts = [];
  for (let i = 0; i <= stops; i++) {
    const t = i / stops;
    const [r, g, b] = TURBO_LUT[Math.round(t * 255)];
    parts.push(`rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}) ${Math.round(t * 100)}%`);
  }
  return `linear-gradient(to right, ${parts.join(", ")})`;
})();
