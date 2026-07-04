// ifc2usd Web ビューワー。
// scene.json を読み込み、GLBの表示・カメラ操作・階層ツリー・表示切替・
// ツリー⇔3D選択同期・ボクセル描画(voxels.json)を行う。

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const viewport = document.getElementById("viewport");
const treePanel = document.getElementById("tree-panel");
const propertyPanel = document.getElementById("property-panel");
const voxelLodSelect = document.getElementById("voxel-lod-select");
const sectionHeightSlider = document.getElementById("section-height-slider");
const sdfSliceToggle = document.getElementById("sdf-slice-toggle");
const sdfSliceHeightSlider = document.getElementById("sdf-slice-height-slider");

const HIGHLIGHT_EMISSIVE = 0x3355ff;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x202020);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);

// preserveDrawingBuffer: E2Eテスト(Playwright)がcanvasを2D canvasへdrawImageして
// ピクセルを読み取れるようにする（既定falseだと描画バッファがcompositing後に
// クリアされうるため、rAFループの外からの読み取りが不安定になる）。このアプリは
// 毎フレーム再描画し続けるため、実ユーザーにまで常時バッファコピーのコストを
// 払わせないよう、URLに`?e2e`が付いているときだけ有効にする
// （E2Eテストは`_wait_for_load`相当のヘルパーでこのクエリ付きURLへ遷移する）。
const isE2ETest = new URLSearchParams(window.location.search).has("e2e");
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: isE2ETest });
renderer.setPixelRatio(window.devicePixelRatio);
viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.2));
const directionalLight = new THREE.DirectionalLight(0xffffff, 1.0);
directionalLight.position.set(5, 10, 7);
scene.add(directionalLight);

// USD は Z-UP、three.js は既定 Y-UP のため、モデルルートを X軸-90度回転して
// 吸収する（--y-up 変換したUSD/GLBを渡した場合はこの回転が不要になるが、
// scene.json の upAxis を見て判定する）。
const modelRoot = new THREE.Group();
scene.add(modelRoot);

function applyUpAxis(upAxis) {
  modelRoot.rotation.x = upAxis === "Y" ? 0 : -Math.PI / 2;
}

function resize() {
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  if (width === 0 || height === 0) return;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}
// window の resize イベントだけでは、将来ツリー/プロパティパネル(E3-4/E3-5)の
// 開閉で #viewport 自体のサイズが変わってもウィンドウ自体は変化しないため
// 検知できない。ResizeObserver で要素自体のサイズ変化を直接監視する。
new ResizeObserver(resize).observe(viewport);

/**
 * カメラを box が画面に収まる位置へ移動する（全体フィット/選択フィットの
 * 共通実装。選択フィットは E3-5 でこの関数へ選択対象のBox3を渡す形で使う）。
 */
function fitCameraToBox(box, { paddingFactor = 1.2 } = {}) {
  if (box.isEmpty()) return;

  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 1e-6);
  const fitDistance =
    (maxDim / 2 / Math.tan((camera.fov * Math.PI) / 360)) * paddingFactor;

  const direction = new THREE.Vector3().subVectors(camera.position, controls.target);
  if (direction.lengthSq() < 1e-9) {
    direction.set(1, 1, 1);
  }
  direction.normalize();

  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(direction, fitDistance);
  updateClipPlanes();
  controls.update();
}

let modelBoundingBox = new THREE.Box3();

function fitAll() {
  fitCameraToBox(modelBoundingBox);
}

// 断面（Z高さ）クリップ平面。normal=(0,-1,0)なので distance = -y + constant となり、
// レンダラーは distance<0（= y>constant のワールド座標）のフラグメントを切り捨てる。
// つまりスライダーは「その高さまで（それより上を隠す）」の断面を表す。
// modelRootの回転（Z-UP→Y-UPの吸収）はメッシュ/ボクセルの頂点座標そのものに
// 効くため、このプレーンはワールド(three.jsのY)座標系で常に水平＝階層を反映する。
//
// constantの初期値はInfinityではなく大きな有限値にする: three.jsは平面をGPUへ
// 送る際に coplanarPoint = normal * (-constant) を計算しており、
// (-1) * (-Infinity) = Infinity * 0 が NaN になるため、モデル読み込み前の毎フレーム
// 描画でNaNをシェーダーuniformへ渡し続けてしまう（実害はまだ無いが不健全）。
const _NO_CLIP_SENTINEL = 1e12;
const sectionClipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), _NO_CLIP_SENTINEL);
renderer.clippingPlanes = [sectionClipPlane];

function setSectionClipHeight(height) {
  sectionClipPlane.constant = height;
  if (Number(sectionHeightSlider.value) !== height) {
    sectionHeightSlider.value = String(height);
  }
}

function initSectionClipRange(box) {
  if (box.isEmpty()) return;
  sectionHeightSlider.min = String(box.min.y);
  sectionHeightSlider.max = String(box.max.y);
  sectionHeightSlider.step = String(Math.max((box.max.y - box.min.y) / 200, 1e-6));
  sectionHeightSlider.disabled = false;
  // 既定はスライダー最大＝クリップなし（モデル全体が見える状態）。
  setSectionClipHeight(box.max.y);
}

sectionHeightSlider.addEventListener("input", () => {
  sectionClipPlane.constant = Number(sectionHeightSlider.value);
});

/**
 * 現在のカメラ-ターゲット距離に応じてnear/farを更新する。fitCameraToBoxは
 * フィット時点の距離でnear/farを設定するだけなので、そのあとホイールで
 * ズームインするとnearを突き抜けてジオメトリが欠けて見える問題が起きる。
 * 毎フレーム呼ぶことで、自由なズーム操作でも近接クリップを避ける。
 */
function updateClipPlanes() {
  const distance = camera.position.distanceTo(controls.target);
  if (distance < 1e-6) return;
  camera.near = Math.max(distance / 100, 0.001);
  camera.far = Math.max(distance * 100, camera.near * 10);
  camera.updateProjectionMatrix();
}

// guid -> THREE.Object3D。GLTFLoaderがglTFノードのextrasを
// object.userData へ直接展開するため、userData.guid で引ける
// （gltf.py が各ノードに書き込む extras.guid が結合キー、spec.md §4.1）。
const objectsByGuid = new Map();

function buildObjectsByGuid() {
  objectsByGuid.clear();
  modelRoot.traverse((obj) => {
    if (obj.userData && obj.userData.guid) {
      objectsByGuid.set(obj.userData.guid, obj);
    }
  });
}

function setObjectVisible(guid, visible) {
  const obj = objectsByGuid.get(guid);
  if (obj) obj.visible = visible;
}

function forEachMeshOf(guid, callback) {
  const obj = objectsByGuid.get(guid);
  if (!obj) return;
  obj.traverse((child) => {
    if (child.isMesh && child.material) callback(child);
  });
}

// guid -> scene.json のツリーノード（class/customData等）。プロパティパネル表示用
// （3Dやツリーの選択と違い、objectsByGuidにはUSDのcustomDataが載っていないため別管理）。
const nodesByGuid = new Map();

function buildNodesByGuid(tree) {
  nodesByGuid.clear();
  function walk(nodes) {
    for (const node of nodes) {
      nodesByGuid.set(node.guid, node);
      walk(node.children);
    }
  }
  walk(tree);
}

function getBoundingBoxOfGuid(guid) {
  const obj = objectsByGuid.get(guid);
  if (!obj) return new THREE.Box3();
  return new THREE.Box3().setFromObject(obj);
}

function renderPropertyPanel(guid) {
  propertyPanel.innerHTML = "";
  if (guid === null) return;

  const node = nodesByGuid.get(guid);
  if (!node) return;

  const dl = document.createElement("dl");
  for (const [key, value] of Object.entries(node.customData)) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  propertyPanel.appendChild(dl);
}

let selectedGuid = null;
// GLTFLoaderがロードしたメッシュ階層のルート。クリック選択のレイキャスト対象を
// voxelRoot(ボクセルInstancedMesh)と切り分けるために使う。loadScene()で設定する。
let glbRoot = null;

function highlightMesh(mesh, on) {
  // gltf.py/usd.py never emit multi-material meshes (one PBRMaterial per mesh),
  // so mesh.material is always a single material here, never an Array.
  if (!mesh.material.emissive) return;
  if (on) {
    if (mesh.userData.__originalEmissive === undefined) {
      mesh.userData.__originalEmissive = mesh.material.emissive.getHex();
    }
    mesh.material.emissive.setHex(HIGHLIGHT_EMISSIVE);
  } else if (mesh.userData.__originalEmissive !== undefined) {
    mesh.material.emissive.setHex(mesh.userData.__originalEmissive);
  }
}

function selectByGuid(guid) {
  // Re-clicking the already-selected node is a no-op by design: this issue's
  // scope is one-directional tree -> 3D sync, not a deselect/toggle affordance.
  if (selectedGuid === guid) return;

  if (selectedGuid !== null) {
    forEachMeshOf(selectedGuid, (mesh) => highlightMesh(mesh, false));
    const prevLi = treePanel.querySelector(`li[data-guid="${selectedGuid}"]`);
    if (prevLi) prevLi.classList.remove("selected");
  }

  selectedGuid = guid;

  if (guid !== null) {
    forEachMeshOf(guid, (mesh) => highlightMesh(mesh, true));
    const li = treePanel.querySelector(`li[data-guid="${guid}"]`);
    if (li) li.classList.add("selected");
  }

  renderPropertyPanel(guid);
  setSdfSliceUiForSelection(guid);
}

// 要素ごとの narrow-band SDF 水平スライス（E5-3、sdf_slice.py が生成する
// `<stem>_sdf.json`）。既定では生成されない（serve --sdf-slices指定時のみ）ため
// scene.jsonにassets.sdfが無ければ空のまま。
const sdfSlicesByGuid = new Map();
// 選択中要素のスライスをオーバーレイ表示するプレーン。選択/トグル/高さ変更の
// たびに作り直す（voxels.jsonのInstancedMeshと違い要素選択のたびに解像度・
// 位置が変わるため、使い回すより毎回張り替える方が単純で扱いやすい）。
let sdfSliceMesh = null;

function _sdfSliceColorFor(value) {
  if (value === null || value === undefined) return [0, 0, 0, 0]; // narrow-band外: 透明
  if (Math.abs(value) < 1e-9) return [255, 255, 255, 230]; // 表面(距離0): 白
  return value < 0 ? [80, 140, 255, 170] : [255, 120, 80, 130]; // 内部: 青 / 外部: 橙
}

function _buildSdfSliceTexture(entry, sliceIndex) {
  const slice = entry.slices[sliceIndex];
  const canvas = document.createElement("canvas");
  canvas.width = entry.cols;
  canvas.height = entry.rows;
  const ctx = canvas.getContext("2d");
  const image = ctx.createImageData(entry.cols, entry.rows);
  for (let row = 0; row < entry.rows; row++) {
    for (let col = 0; col < entry.cols; col++) {
      const [r, g, b, a] = _sdfSliceColorFor(slice.values[row][col]);
      const i = (row * entry.cols + col) * 4;
      image.data[i] = r;
      image.data[i + 1] = g;
      image.data[i + 2] = b;
      image.data[i + 3] = a;
    }
  }
  ctx.putImageData(image, 0, 0);
  const texture = new THREE.CanvasTexture(canvas);
  texture.magFilter = THREE.NearestFilter;
  texture.minFilter = THREE.NearestFilter;
  return texture;
}

function clearSdfSliceOverlay() {
  if (!sdfSliceMesh) return;
  modelRoot.remove(sdfSliceMesh);
  sdfSliceMesh.geometry.dispose();
  sdfSliceMesh.material.map?.dispose();
  sdfSliceMesh.material.dispose();
  sdfSliceMesh = null;
}

// PlaneGeometryは既定でXY平面(法線+Z)に置かれる。sdf_slice.pyのorigin/z値は
// IFCのZ-UPワールド座標そのもの(voxelRootの各インスタンスと同じ規約)なので、
// modelRootの子として追加すれば、他の描画物と同じZ-UP→Y-UP回転を受けて
// 正しい向き・高さで表示される(平面自体を回転させる必要は無い)。
function updateSdfSliceOverlay() {
  clearSdfSliceOverlay();
  if (!sdfSliceToggle.checked || selectedGuid === null) return;

  const entry = sdfSlicesByGuid.get(selectedGuid);
  if (!entry) return;

  const sliceIndex = Number(sdfSliceHeightSlider.value);
  const slice = entry.slices[sliceIndex];
  if (!slice) return;

  const texture = _buildSdfSliceTexture(entry, sliceIndex);
  const geometry = new THREE.PlaneGeometry(entry.cols * entry.size, entry.rows * entry.size);
  // depthTest:false: このスライスは選択中要素自身の内部を切った断面なので、
  // 通常の深度テストのままだと要素自身の不透明メッシュ/ボクセルに埋もれて
  // 常に隠れてしまう（診断目的のオーバーレイなので、他ジオメトリより手前に
  // 常時見える方が実用上正しい）。
  const material = new THREE.MeshBasicMaterial({
    map: texture,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    side: THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.renderOrder = 999;
  mesh.position.set(
    entry.originX + (entry.cols * entry.size) / 2,
    entry.originY + (entry.rows * entry.size) / 2,
    slice.z,
  );
  modelRoot.add(mesh);
  sdfSliceMesh = mesh;
}

function setSdfSliceUiForSelection(guid) {
  const entry = guid !== null ? sdfSlicesByGuid.get(guid) : undefined;
  if (!entry) {
    sdfSliceToggle.checked = false;
    sdfSliceToggle.disabled = true;
    sdfSliceHeightSlider.disabled = true;
    sdfSliceHeightSlider.min = "0";
    sdfSliceHeightSlider.max = "0";
    sdfSliceHeightSlider.value = "0";
    clearSdfSliceOverlay();
    return;
  }
  sdfSliceToggle.disabled = false;
  sdfSliceHeightSlider.disabled = false;
  sdfSliceHeightSlider.min = "0";
  sdfSliceHeightSlider.max = String(entry.slices.length - 1);
  sdfSliceHeightSlider.step = "1";
  sdfSliceHeightSlider.value = String(Math.floor(entry.slices.length / 2));
  updateSdfSliceOverlay();
}

sdfSliceToggle.addEventListener("change", updateSdfSliceOverlay);
sdfSliceHeightSlider.addEventListener("input", updateSdfSliceOverlay);

async function loadSdfSlices(sdfUrl) {
  const response = await fetch(sdfUrl);
  if (!response.ok) {
    throw new Error(`failed to load sdf slices: ${response.status}`);
  }
  const data = await response.json();
  sdfSlicesByGuid.clear();
  for (const [guid, entry] of Object.entries(data.elements)) {
    sdfSlicesByGuid.set(guid, entry);
  }
}

// voxels.json のLODごとに1つの THREE.InstancedMesh を割り当てる（1 draw call/LOD、
// NFR-2）。要素ごとの色は per-instance color として反映する。
const voxelRoot = new THREE.Group();
modelRoot.add(voxelRoot);
const voxelLods = [];

// JSのシフト演算子(<<, >>)はシフト量を32で割った余りとして扱うため、シフト量が
// 32以上になると0を返さずラップアラウンドしてしまう（コード自体が32bitに収まる
// かどうかとは別の制約）。ループは `code >> (3*i)` が0になるまで回るため、コードの
// 最上位ビット位置Lに対し最終的に評価するシフト量は 3*ceil(L/3) になる。これが
// 31以下に収まる最大のLは30（ceil(30/3)*3=30）なので、閾値は2^30-1に取る
// （2^31-1まで許すと31bit境界でシフト量が33になりラップアラウンドして壊れる）。
const _MORTON_FAST_PATH_MAX_CODE = 0x3fffffff;

function mortonDecode(code) {
  if (code <= _MORTON_FAST_PATH_MAX_CODE) {
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

  // spec.md §2は3軸21bitまで(=最大63bit)のMortonコードを許容するが、JSのビット
  // 演算子(<<, |, &)は32bit符号付き整数に丸められ、それを超えるビットが破壊される。
  // ifc2usd/voxel.py の morton_decode と同じアルゴリズムをBigIntで実装し直すことで、
  // 63bit全域を精度劣化・破壊なく復元できるようにする（上のfast pathを超えた
  // まれなケースのみ、より遅いBigIntを使う）。
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

const _voxelUnitBox = new THREE.BoxGeometry(1, 1, 1);

function buildVoxelLods(voxelDescription) {
  const origin = voxelDescription.origin;
  const matrix = new THREE.Matrix4();
  const color = new THREE.Color();

  // loadScene()はページ読み込みにつき一度しか呼ばないため今は再構築されないが、
  // 将来モデルの再読み込み経路が増えたときに<option>やvoxelLodsが際限なく
  // 重複しないよう、念のため呼び出しごとに初期化しておく。ジオメトリ
  // (_voxelUnitBox)は全LOD/全呼び出しで共有する1つのBoxGeometryなので
  // disposeしない。マテリアルはmesh単位で毎回新規生成しているため、こちらは破棄する。
  for (const mesh of voxelRoot.children) mesh.material?.dispose?.();
  voxelRoot.clear();
  voxelLods.length = 0;
  voxelLodSelect.innerHTML = "";

  for (const lod of voxelDescription.lods) {
    const size = lod.size;
    const totalInstances = lod.elements.reduce((sum, el) => sum + el.indices.length, 0);

    const material = new THREE.MeshStandardMaterial({ vertexColors: true });
    const mesh = new THREE.InstancedMesh(_voxelUnitBox, material, totalInstances);
    const instanceGuids = new Array(totalInstances);

    let instanceIndex = 0;
    for (const el of lod.elements) {
      color.setRGB(el.color[0], el.color[1], el.color[2]);
      for (const code of el.indices) {
        const [ix, iy, iz] = mortonDecode(code);
        matrix.makeScale(size, size, size);
        matrix.setPosition(
          origin[0] + (ix + 0.5) * size,
          origin[1] + (iy + 0.5) * size,
          origin[2] + (iz + 0.5) * size,
        );
        mesh.setMatrixAt(instanceIndex, matrix);
        mesh.setColorAt(instanceIndex, color);
        instanceGuids[instanceIndex] = el.guid;
        instanceIndex++;
      }
    }
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    // 初期visibleは常にfalseにしておき、実際の可視状態はapplyDisplayState()に
    // 一元化する（表示モード/アクティブLODの決定ロジックを1箇所にまとめるため）。
    mesh.visible = false;

    voxelRoot.add(mesh);
    voxelLods.push({ size, mesh, instanceGuids });

    const option = document.createElement("option");
    option.value = String(voxelLods.length - 1);
    option.textContent = `${size}m`;
    voxelLodSelect.appendChild(option);
  }
}

// "mesh" | "voxel" | "both"。複数LODは同じ体積を異なる粒度で表現したものなので、
// voxel/bothモードでも常にactiveVoxelLodIndexの1つだけを可視にする。
// 既定値はindex.html側の<input checked>から読み取る（ハードコードして二重管理
// すると、片方だけ書き換えたときにUI表示と実際の状態がずれてしまうため）。
const _checkedDisplayModeInput = document.querySelector('input[name="display-mode"]:checked');
let displayMode = _checkedDisplayModeInput ? _checkedDisplayModeInput.value : "both";
let activeVoxelLodIndex = 0;

function applyDisplayState() {
  if (glbRoot) {
    glbRoot.visible = displayMode === "mesh" || displayMode === "both";
  }
  const showVoxels = displayMode === "voxel" || displayMode === "both";
  voxelLods.forEach((lod, index) => {
    lod.mesh.visible = showVoxels && index === activeVoxelLodIndex;
  });
}

function setDisplayMode(mode) {
  displayMode = mode;
  applyDisplayState();
}

function setActiveVoxelLodIndex(index) {
  activeVoxelLodIndex = index;
  applyDisplayState();
}

for (const input of document.querySelectorAll('input[name="display-mode"]')) {
  input.addEventListener("change", (event) => {
    if (event.target.checked) setDisplayMode(event.target.value);
  });
}

voxelLodSelect.addEventListener("change", () => {
  setActiveVoxelLodIndex(Number(voxelLodSelect.value));
});

async function loadVoxels(voxelsUrl) {
  const response = await fetch(voxelsUrl);
  if (!response.ok) {
    throw new Error(`failed to load voxels: ${response.status}`);
  }
  const voxelDescription = await response.json();
  buildVoxelLods(voxelDescription);
  applyDisplayState();
}

function findGuidOfObject(object) {
  let current = object;
  while (current) {
    if (current.userData && current.userData.guid) return current.userData.guid;
    current = current.parent;
  }
  return null;
}

function findGuidOfVoxelInstance(mesh, instanceId) {
  for (const lod of voxelLods) {
    // findGuidOfObject と挙動を揃えるため null で統一する（undefined を返すと
    // 呼び出し側の `guid !== null` ガードを素通りしてしまい、selectByGuid が
    // undefined で呼ばれて既存の選択状態を壊しかねない）。
    if (lod.mesh === mesh) return lod.instanceGuids[instanceId] ?? null;
  }
  return null;
}

// 表示モードに応じたクリック選択レイキャストの対象を返す。
//
// object.visible をレイキャスト対象の絞り込みに使わない: three.jsの
// Raycaster は祖先の visible を辿らず、各ノード自身の visible だけを見て
// 判定する（Groupのvisible=falseは描画上は子を隠すが、レイキャストには
// 影響しない）。glbRoot(GLTFLoaderのルートGroup)のvisibleだけをfalseに
// してもその子メッシュ自身はvisible=trueのままなので、voxelモードで
// メッシュが「見えないのにクリックだけは反応する」事故になる。
// そのためdisplayMode/activeVoxelLodIndexから対象リストを明示的に組み立てる。
function currentRaycastTargets() {
  const targets = [];
  if (glbRoot && (displayMode === "mesh" || displayMode === "both")) {
    targets.push(glbRoot);
  }
  if (displayMode === "voxel" || displayMode === "both") {
    const activeLod = voxelLods[activeVoxelLodIndex];
    if (activeLod) targets.push(activeLod.mesh);
  }
  return targets;
}

const raycaster = new THREE.Raycaster();
const pointerNdc = new THREE.Vector2();
let pointerDownPosition = null;

// OrbitControlsのドラッグ操作でも同じ要素上でpointerdown/upが発火するため、
// 移動距離が小さい（=ドラッグではなくクリック）場合のみ選択レイキャストを行う。
const CLICK_DRAG_THRESHOLD_PX = 5;

renderer.domElement.addEventListener("pointerdown", (event) => {
  // isPrimary除外でマルチタッチの2本目以降を無視。button!==0除外で右クリック
  // (OrbitControlsのpan操作)・中クリックを選択レイキャストの対象から外す。
  if (!event.isPrimary || event.button !== 0) return;
  pointerDownPosition = { x: event.clientX, y: event.clientY };
});

renderer.domElement.addEventListener("pointercancel", () => {
  pointerDownPosition = null;
});

renderer.domElement.addEventListener("pointerup", (event) => {
  const downPosition = pointerDownPosition;
  pointerDownPosition = null;
  if (!downPosition || !event.isPrimary || event.button !== 0) return;

  const dx = event.clientX - downPosition.x;
  const dy = event.clientY - downPosition.y;
  if (Math.hypot(dx, dy) > CLICK_DRAG_THRESHOLD_PX) return;

  const rect = renderer.domElement.getBoundingClientRect();
  pointerNdc.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointerNdc.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(pointerNdc, camera);
  // 既知の制約（Issue #18時点で未対応、意図的に据え置き）: three.jsのRaycasterは
  // renderer.clippingPlanesを考慮しないため、断面クリップで視覚的に隠れている
  // ジオメトリも普通にクリックで選択できてしまう。FR-10自体は表示上の断面機能で
  // あり選択への影響は要件外のため、このissueでは対応しない。必要になれば
  // ここで `intersections.filter(i => i.point.y <= sectionClipPlane.constant)` の
  // ようなフィルタを追加する。
  const intersections = raycaster.intersectObjects(currentRaycastTargets(), true);
  if (intersections.length === 0) return;

  const hit = intersections[0];
  const guid = hit.object.isInstancedMesh
    ? findGuidOfVoxelInstance(hit.object, hit.instanceId)
    : findGuidOfObject(hit.object);
  if (guid !== null) selectByGuid(guid);
});

function renderTreeNode(node) {
  const li = document.createElement("li");
  li.dataset.guid = node.guid;

  const visibility = document.createElement("input");
  visibility.type = "checkbox";
  visibility.className = "tree-visibility";
  visibility.checked = true;
  visibility.addEventListener("change", () => setObjectVisible(node.guid, visibility.checked));
  li.appendChild(visibility);

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = node.name ? `${node.name} (${node.class})` : node.class;
  label.addEventListener("click", () => selectByGuid(node.guid));
  li.appendChild(label);

  if (node.children && node.children.length > 0) {
    const ul = document.createElement("ul");
    for (const child of node.children) {
      ul.appendChild(renderTreeNode(child));
    }
    li.appendChild(ul);
  }

  return li;
}

function renderTree(tree) {
  treePanel.innerHTML = "";
  const ul = document.createElement("ul");
  ul.className = "tree-root";
  for (const node of tree) {
    ul.appendChild(renderTreeNode(node));
  }
  treePanel.appendChild(ul);
}

async function loadScene() {
  const response = await fetch("./scene.json");
  if (!response.ok) {
    throw new Error(`failed to load scene.json: ${response.status}`);
  }
  const sceneDescription = await response.json();

  applyUpAxis(sceneDescription.upAxis);

  const loader = new GLTFLoader();
  const gltf = await loader.loadAsync(sceneDescription.assets.gltf);
  modelRoot.add(gltf.scene);
  glbRoot = gltf.scene;
  applyDisplayState();

  modelBoundingBox = new THREE.Box3().setFromObject(modelRoot);
  fitAll();
  initSectionClipRange(modelBoundingBox);

  buildObjectsByGuid();
  buildNodesByGuid(sceneDescription.tree);
  renderTree(sceneDescription.tree);

  if (sceneDescription.assets.voxels) {
    // ボクセルはメッシュ表示にとって付加的な情報（サーバー側もvoxels.jsonが
    // 無ければassetsから省く設計）なので、読み込み失敗はメッシュ表示自体を
    // 巻き込んではいけない。ここだけ個別にcatchし、警告に留めて続行する。
    try {
      await loadVoxels(sceneDescription.assets.voxels);
    } catch (error) {
      console.warn("ifc2usd viewer: failed to load voxels, continuing without them", error);
    }
  }

  if (sceneDescription.assets.sdf) {
    // sdfもボクセル同様、無ければメッシュ表示自体には影響しない付加情報。
    try {
      await loadSdfSlices(sceneDescription.assets.sdf);
    } catch (error) {
      console.warn("ifc2usd viewer: failed to load SDF slices, continuing without them", error);
    }
  }

  return sceneDescription;
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  updateClipPlanes();
  renderer.render(scene, camera);
}

// Playwright/E2Eテスト、および後続issue（E3-5）が使うフック。
window.ifc2usdViewer = {
  scene,
  camera,
  controls,
  renderer,
  modelRoot,
  fitCameraToBox,
  fitAll,
  getBoundingBox: () => modelBoundingBox,
  getBoundingBoxOfGuid,
  selectByGuid,
  getSelectedGuid: () => selectedGuid,
  voxelLods,
  mortonDecode,
  getGlbRoot: () => glbRoot,
  setDisplayMode,
  getDisplayMode: () => displayMode,
  setActiveVoxelLodIndex,
  currentRaycastTargets,
  getSectionClipHeight: () => sectionClipPlane.constant,
  setSectionClipHeight,
  hasSdfSlicesFor: (guid) => sdfSlicesByGuid.has(guid),
  getSdfSliceMesh: () => sdfSliceMesh,
};

resize();
animate();

loadScene()
  .then((sceneDescription) => {
    window.ifc2usdViewer.sceneDescription = sceneDescription;
    window.ifc2usdLoaded = true;
  })
  .catch((error) => {
    console.error("ifc2usd viewer: failed to load scene", error);
    window.ifc2usdLoadError = String(error);

    const banner = document.createElement("div");
    banner.id = "load-error-banner";
    banner.style.cssText =
      "position:absolute;top:0;left:0;right:0;padding:12px;" +
      "background:#5a1e1e;color:#fff;font-family:sans-serif;z-index:10;";
    banner.textContent = `モデルの読み込みに失敗しました: ${error}`;
    viewport.appendChild(banner);
  });
