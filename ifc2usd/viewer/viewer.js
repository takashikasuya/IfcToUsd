// ifc2usd Web ビューワー。
// scene.json を読み込み、GLB表示・階層ツリー・選択を実装していく
// （E3-3/E3-4/E3-5 で段階的に肉付けする）。

async function main() {
  const response = await fetch("./scene.json");
  const scene = await response.json();
  console.log("ifc2usd viewer: loaded scene.json", scene);
}

main();
