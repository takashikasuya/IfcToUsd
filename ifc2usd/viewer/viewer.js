// ifc2usd Web ビューワー。
// scene.json を読み込み、GLBの表示・カメラ操作・階層ツリー・表示切替・
// ツリー⇔3D選択同期・ボクセル描画(voxels.json)を行う。

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const viewport = document.getElementById("viewport");
const treePanel = document.getElementById("tree-panel");
const treeNodesContainer = document.getElementById("tree-nodes");
const treeSearchInput = document.getElementById("tree-search-input");
const propertyPanel = document.getElementById("property-panel");
const voxelLodSelect = document.getElementById("voxel-lod-select");
const sectionHeightSlider = document.getElementById("section-height-slider");
const sdfSliceToggle = document.getElementById("sdf-slice-toggle");
const sdfSliceHeightSlider = document.getElementById("sdf-slice-height-slider");
const wireframeToggle = document.getElementById("wireframe-toggle");
const ghostToggle = document.getElementById("ghost-toggle");
const treePanelToggle = document.getElementById("tree-panel-toggle");
const propertyPanelToggle = document.getElementById("property-panel-toggle");
const shortcutsOverlay = document.getElementById("shortcuts-overlay");
const liveToolbarGroup = document.getElementById("live-toolbar-group");
const liveToolbarDivider = document.getElementById("live-toolbar-divider");
const liveMetricSelect = document.getElementById("live-metric-select");
const liveToggle = document.getElementById("live-toggle");
const livePauseToggle = document.getElementById("live-pause-toggle");
const liveLegend = document.getElementById("live-legend");
const liveLegendGradientEl = document.getElementById("live-legend-gradient");
const liveLegendMinLabel = document.getElementById("live-legend-min");
const liveLegendMaxLabel = document.getElementById("live-legend-max");
const liveLegendUnitLabel = document.getElementById("live-legend-unit");

// デザイントークン(E8-5 / ux-spec.md §3.5): 色はindex.htmlの:rootカスタム
// プロパティを唯一の定義source とし、ここではgetComputedStyleで初期化時に
// 1回だけ解決する(index.html側の値を変えるだけでビューワー内の色も揃うように、
// 二重管理を避ける)。
function _cssVar(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

const HIGHLIGHT_EMISSIVE = new THREE.Color(_cssVar("--accent", "#3355ff")).getHex();

const scene = new THREE.Scene();
scene.background = new THREE.Color(_cssVar("--bg", "#202020"));

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
    // "__outline"は_showMeshOutlineが追加する装飾用の子メッシュ(isMesh===true)。
    // 除外しないと再選択のたびにforEachMeshOf自身の走査で拾われ、outline-of-
    // outlineが無限に増殖してスタックオーバーフローする。
    if (child.isMesh && child.material && child.name !== "__outline") callback(child);
  });
}

// guid -> scene.json のツリーノード（class/customData等）。プロパティパネル表示用
// （3Dやツリーの選択と違い、objectsByGuidにはUSDのcustomDataが載っていないため別管理）。
const nodesByGuid = new Map();
// guid -> 親のguid（ルート直下はnull）。選択時の祖先自動展開(E8-3)・検索での
// 祖先表示・isolateの子孫判定に使う。
const _parentGuidByGuid = new Map();
// guid -> 対応する<li>要素。折りたたみ状態やisolateクラスの外部操作に使う
// （renderTreeNode内のクロージャ変数に頼らずDOM状態を単一の真実source にする）。
const _liByGuid = new Map();

function buildNodesByGuid(tree) {
  nodesByGuid.clear();
  _parentGuidByGuid.clear();
  function walk(nodes, parentGuid) {
    for (const node of nodes) {
      nodesByGuid.set(node.guid, node);
      _parentGuidByGuid.set(node.guid, parentGuid);
      walk(node.children, node.guid);
    }
  }
  walk(tree, null);
}

function getBoundingBoxOfGuid(guid) {
  const obj = objectsByGuid.get(guid);
  if (!obj) return new THREE.Box3();
  return new THREE.Box3().setFromObject(obj);
}

// scene_index.pyの_METADATA_KEYSと同じ定義順(E8-4 / ux-spec.md §3.4)。
// customDataオブジェクトのキー順もPython側で既にこの順で挿入されているが、
// 表示側の意図を明示するため独立して定義する。
const PROPERTY_KEY_ORDER = ["GUID", "class", "Name", "LongName", "Description", "Latitude", "Longitude"];

// IfcCompoundPlaneAngleMeasure由来の度.分.秒.(百万分の一秒)ドット区切り文字列
// (usd.py参照)を10進度へ変換する。度が負の場合は南半球/西半球を表し、
// 分・秒は符号無しの大きさとして加算する。
function _dmsStringToDecimalDegrees(dmsString) {
  // usd.pyのset_custom_dataは、IfcSiteにRefLatitude/RefLongitudeが未設定でも
  // Latitude/Longitude customData自体は書き込む(値が空文字列になるだけ)。
  // ""をsplitすると[""]（Number("")は0、NaNではない）になるため、下のNaN
  // ガードだけでは弾けず「0.000000°」という実在しない値を表示してしまう
  // (コードレビューで検出)。空文字列は明示的にnullへ落とし、呼び出し側に
  // 生値(空文字列)へフォールバックさせる。
  if (dmsString === "") return null;
  const parts = String(dmsString).split(".").map(Number);
  if (parts.length === 0 || parts.some((p) => Number.isNaN(p))) return null;
  const [degrees, minutes = 0, seconds = 0, millionths = 0] = parts;
  const sign = degrees < 0 ? -1 : 1;
  return sign * (Math.abs(degrees) + minutes / 60 + (seconds + millionths / 1e6) / 3600);
}

function _copyTextToClipboard(text, onDone) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => onDone(true),
      () => onDone(false),
    );
  } else {
    onDone(false);
  }
}

function renderPropertyPanel(guid) {
  propertyPanel.innerHTML = "";

  if (guid === null) {
    const guide = document.createElement("p");
    guide.className = "property-guide";
    guide.textContent = "要素をクリックまたはツリーから選択してください";
    propertyPanel.appendChild(guide);
    return;
  }

  const node = nodesByGuid.get(guid);
  if (!node) return;

  const cd = node.customData;
  const dl = document.createElement("dl");

  for (const key of PROPERTY_KEY_ORDER) {
    if (!(key in cd)) continue;
    const value = cd[key];

    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");

    if (key === "class") {
      const chip = document.createElement("span");
      chip.className = "property-class-chip";
      if (node.color) {
        const [r, g, b] = node.color;
        chip.style.background = `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
      }
      dd.appendChild(chip);
      const displayName = document.createElement("span");
      displayName.textContent = value.startsWith("Ifc") ? value.slice(3) : value;
      dd.appendChild(displayName);
    } else if (key === "GUID") {
      const guidText = document.createElement("span");
      guidText.className = "property-guid-text";
      guidText.textContent = value;
      dd.appendChild(guidText);

      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "property-copy-btn";
      copyBtn.textContent = "Copy";
      copyBtn.title = "Copy GUID";
      copyBtn.addEventListener("click", () => {
        _copyTextToClipboard(value, (ok) => {
          if (ok) {
            copyBtn.textContent = "Copied";
            setTimeout(() => {
              copyBtn.textContent = "Copy";
            }, 1200);
          } else {
            // クリップボードAPIが使えない/失敗した場合のフォールバック:
            // GUIDテキストを選択状態にし、手動コピー(Ctrl+C)できるようにする。
            const range = document.createRange();
            range.selectNodeContents(guidText);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
          }
        });
      });
      dd.appendChild(copyBtn);
    } else if (key === "Latitude" || key === "Longitude") {
      const decimal = _dmsStringToDecimalDegrees(value);
      dd.textContent = decimal !== null ? `${decimal.toFixed(6)}°` : value;
    } else {
      dd.textContent = value;
    }

    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  propertyPanel.appendChild(dl);

  // ビルOS連携デジタルツイン(Epic E9)のLive Dataセクション。twin.jsonが無い/
  // このGUIDにバインディングが無い場合は_updateLiveDataSection内で空のままにする。
  const liveData = document.createElement("div");
  liveData.id = "property-live-data";
  propertyPanel.appendChild(liveData);
  _updateLiveDataSection(guid);
}

let selectedGuid = null;
let hoverGuid = null;
// GLTFLoaderがロードしたメッシュ階層のルート。クリック選択のレイキャスト対象を
// voxelRoot(ボクセルInstancedMesh)と切り分けるために使う。loadScene()で設定する。
let glbRoot = null;

// gltf.pyはマテリアルをメッシュごとに独立生成するが、trimeshのGLBエクスポート時に
// 同一プロパティ値(色/metallic/roughness)のマテリアルは1つに重複排除される。その
// ため同色の複数要素はGLTFLoaderロード後も同一THREE.Material参照を共有しており、
// mesh.materialへの直接変更（emissive/opacity等）は同色の他要素へ波及する
// （E8-1着手前に検証・確認済み）。書き込み前に一度だけcloneして各meshへ
// 専有マテリアルを持たせることで波及を防ぐ。
function _ensureOwnMaterial(mesh) {
  if (!mesh.userData.__ownMaterial) {
    mesh.material = mesh.material.clone();
    mesh.userData.__ownMaterial = true;
  }
}

// emissiveの一時的な変更を1箇所へ集約する。hex===nullは「元の色へ戻す」を表す。
// __originalEmissiveは初回変更時にのみ記録するため、選択・ホバーどちらが先に
// 触っても常に「本当の既定色」を指し続ける（両者は同一meshに同時適用されない
// 設計、後述のsetHoverGuid参照）。
function _setMeshEmissiveTint(mesh, hex) {
  // gltf.py/usd.py never emit multi-material meshes (one PBRMaterial per mesh),
  // so mesh.material is always a single material here, never an Array.
  if (!mesh.material.emissive) return;
  if (hex !== null) {
    _ensureOwnMaterial(mesh);
    if (mesh.userData.__originalEmissive === undefined) {
      mesh.userData.__originalEmissive = mesh.material.emissive.getHex();
    }
    mesh.material.emissive.setHex(hex);
  } else if (mesh.userData.__originalEmissive !== undefined) {
    mesh.material.emissive.setHex(mesh.userData.__originalEmissive);
  }
}

function highlightMesh(mesh, on) {
  _setMeshEmissiveTint(mesh, on ? HIGHLIGHT_EMISSIVE : null);
}

// ホバー表現(E8-2 / ux-spec.md §3.2)。選択より弱い色味・弱いボクセルlerp比率で
// 「予告」を示す。選択中要素へは適用しない(setHoverGuid側でガードする)ため、
// _setMeshEmissiveTint/__originalEmissiveの復元先が選択と競合することはない。
const HOVER_EMISSIVE = 0x222a44;

function setMeshHoverTint(mesh, on) {
  _setMeshEmissiveTint(mesh, on ? HOVER_EMISSIVE : null);
}

// 選択要素の輪郭表示（バックフェイス・ハル方式、ux-spec.md §3.1）。本体の
// 複製(法線方向へわずかに膨らませる)をBackSideで背後に描く。ジオメトリ・親子
// 関係は元メッシュを共有するため、追加のクローン・変換計算コストはdraw call
// 1回分のみで済む。
const OUTLINE_THICKNESS = 0.03;

function _createOutlineMaterial() {
  const material = new THREE.MeshBasicMaterial({ color: HIGHLIGHT_EMISSIVE, side: THREE.BackSide });
  material.onBeforeCompile = (shader) => {
    // objectNormal(法線変換チャンク)はUSE_ENVMAP/USE_SKINNING時のみ宣言されるため
    // 依存できない。全頂点シェーダで無条件宣言される生のattribute vec3 normalを
    // ローカル空間のまま使う（このユースケースでは十分）。
    shader.vertexShader = shader.vertexShader.replace(
      "#include <begin_vertex>",
      `#include <begin_vertex>\n\ttransformed += normalize(normal) * ${OUTLINE_THICKNESS.toFixed(6)};`,
    );
  };
  return material;
}

const _outlineMaterial = _createOutlineMaterial();

function _showMeshOutline(mesh) {
  if (mesh.userData.__outline) return;
  const outline = new THREE.Mesh(mesh.geometry, _outlineMaterial);
  outline.name = "__outline";
  // ハイライト表現専用の装飾オブジェクトなので、クリック選択のレイキャスト対象に
  // 含めない（CLAUDE.md記載のRaycaster規約: 対象は明示リストで組む）。
  outline.raycast = () => {};
  mesh.add(outline);
  mesh.userData.__outline = outline;
}

function _hideMeshOutline(mesh) {
  const outline = mesh.userData.__outline;
  if (!outline) return;
  mesh.remove(outline);
  delete mesh.userData.__outline;
}

// ボクセル側の輪郭表示。E7-3のinstanceIndicesByGuidを流用し、選択要素のインスタンス
// だけを含む一時的なInstancedMeshを~1.06倍スケールのBackSideで生成する
// （ux-spec.md §3.1）。
const _voxelOutlineScaleMatrix = new THREE.Matrix4().makeScale(1.06, 1.06, 1.06);

function _showVoxelOutline(guid) {
  const matrix = new THREE.Matrix4();
  for (const lod of voxelLods) {
    const indices = lod.instanceIndicesByGuid.get(guid);
    if (!indices || indices.length === 0) continue;

    const outlineMesh = new THREE.InstancedMesh(_voxelUnitBox, _outlineMaterial, indices.length);
    for (let i = 0; i < indices.length; i++) {
      lod.mesh.getMatrixAt(indices[i], matrix);
      // 並進+スケールのみ(回転無し)の行列なので、対角スケール行列同士は可換:
      // 中心位置を保ったまま一様スケールだけ~1.06倍される。
      matrix.multiply(_voxelOutlineScaleMatrix);
      outlineMesh.setMatrixAt(i, matrix);
    }
    outlineMesh.instanceMatrix.needsUpdate = true;
    outlineMesh.raycast = () => {};
    voxelRoot.add(outlineMesh);
    lod.outlineMesh = outlineMesh;
  }
}

function _hideVoxelOutline() {
  for (const lod of voxelLods) {
    if (!lod.outlineMesh) continue;
    voxelRoot.remove(lod.outlineMesh);
    lod.outlineMesh.dispose();
    lod.outlineMesh = null;
  }
}

// forEachMeshOf/highlightMeshはGLTFLoaderのメッシュ階層(objectsByGuid)にしか作用しない。
// ボクセル専用表示モード(メッシュ非表示)では、そちらをemissiveで光らせても画面に
// 何も見えないため、選択状態を示せない(Issue #37)。InstancedMeshは要素単位の
// 個別Object3Dを持たないため、per-instance色(instanceColor)を選択中のインスタンス
// だけ一時的に書き換えることでハイライトを表現する。
const _voxelHighlightColor = new THREE.Color(HIGHLIGHT_EMISSIVE);
const _voxelHoverColor = new THREE.Color(HOVER_EMISSIVE);
const _voxelHighlightScratch = new THREE.Color();

// 元の要素色を保ったまま識別できるよう、対象色へ寄せるだけで完全には置き換え
// ない（element色そのものが手がかりの一部のため）。ratioが小さいほど「弱い」
// 表現になる（ホバーは選択より弱くする、ux-spec.md §3.2）。
function _tintVoxelInstancesOfGuid(guid, color, ratio, on) {
  for (const lod of voxelLods) {
    const indices = lod.instanceIndicesByGuid.get(guid);
    if (!indices || !lod.originalColors) continue;

    for (const i of indices) {
      _voxelHighlightScratch.fromArray(lod.originalColors, i * 3);
      if (on) _voxelHighlightScratch.lerp(color, ratio);
      lod.mesh.setColorAt(i, _voxelHighlightScratch);
    }
    lod.mesh.instanceColor.needsUpdate = true;
  }
}

function highlightVoxelInstancesOfGuid(guid, on) {
  _tintVoxelInstancesOfGuid(guid, _voxelHighlightColor, 0.6, on);
}

function hoverVoxelInstancesOfGuid(guid, on) {
  _tintVoxelInstancesOfGuid(guid, _voxelHoverColor, 0.3, on);
}

function selectByGuid(guid) {
  // Re-clicking the already-selected node is a no-op by design: this issue's
  // scope is one-directional tree -> 3D sync, not a deselect/toggle affordance.
  if (selectedGuid === guid) return;

  if (selectedGuid !== null) {
    forEachMeshOf(selectedGuid, (mesh) => {
      highlightMesh(mesh, false);
      _hideMeshOutline(mesh);
    });
    highlightVoxelInstancesOfGuid(selectedGuid, false);
    _hideVoxelOutline();
    const prevLi = treePanel.querySelector(`li[data-guid="${selectedGuid}"]`);
    if (prevLi) prevLi.classList.remove("selected");

    // 選択解除された要素がなお(マウスが動いていないため)ホバー中なら、選択表現に
    // 譲っていた弱いホバー表現をここで再適用する。さもないと、選択解除の瞬間
    // カーソルはまだその要素の上にあるのに、ホバー表現もハイライト表現も
    // 付かない状態になってしまう(次にpointermoveが起きるまで気付けない)。
    if (selectedGuid === hoverGuid) {
      forEachMeshOf(selectedGuid, (mesh) => setMeshHoverTint(mesh, true));
      hoverVoxelInstancesOfGuid(selectedGuid, true);
    }
  }

  selectedGuid = guid;

  if (guid !== null) {
    forEachMeshOf(guid, (mesh) => {
      // ゴーストモード中に「非選択(ゴースト済み)要素」を新たに選択した場合、
      // mesh.materialが共有の_ghostMaterial(MeshBasicMaterial、emissive無し)を
      // 指したままhighlightMeshを呼ぶと、_ensureOwnMaterial/emissive設定が
      // 静かにスキップされてしまう(コードレビューで検出)。ハイライト適用前に
      // 選択要素自身のゴーストを先に解除しておく。末尾の_applyGhostState()は
      // 「非選択要素」側のゴースト状態を整えるためのものなので、この解除とは
      // 重複しない。
      _setMeshGhosted(mesh, false);
      highlightMesh(mesh, true);
      _showMeshOutline(mesh);
    });
    highlightVoxelInstancesOfGuid(guid, true);
    _showVoxelOutline(guid);
    const li = treePanel.querySelector(`li[data-guid="${guid}"]`);
    if (li) {
      li.classList.add("selected");
      // E8-3: 選択行まで祖先を自動展開し、スクロールして見えるようにする
      // (ホバーではスクロールしない、E8-2との違い)。
      _expandAncestorsOf(guid);
      li.scrollIntoView({ block: "nearest" });
    }
  }

  renderPropertyPanel(guid);
  setSdfSliceUiForSelection(guid);
  _applyGhostState();
}

function _setTreeRowHovered(guid, on) {
  const li = treePanel.querySelector(`li[data-guid="${guid}"]`);
  if (li) li.classList.toggle("hovered", on);
}

// ホバー状態の唯一の変更経路（E8-2）。3D側のpointermoveレイキャストとツリー行の
// mouseenter/leaveの両方がこの関数を呼ぶため、3D→ツリー・ツリー→3Dの双方向連携が
// 自然に揃う。選択中要素へは3D側のエミッシブ/ボクセル色を変更しない
// （「選択中要素へのホバーは選択表現を優先する」）が、ツリー行の.hoveredクラス
// 自体は選択有無に関わらず切り替える（ホバーの予告自体は選択中でも見せてよい）。
function setHoverGuid(guid) {
  if (guid === hoverGuid) return;

  if (hoverGuid !== null) {
    _setTreeRowHovered(hoverGuid, false);
    if (hoverGuid !== selectedGuid) {
      forEachMeshOf(hoverGuid, (mesh) => setMeshHoverTint(mesh, false));
      hoverVoxelInstancesOfGuid(hoverGuid, false);
    }
  }

  hoverGuid = guid;

  if (hoverGuid !== null) {
    _setTreeRowHovered(hoverGuid, true);
    if (hoverGuid !== selectedGuid) {
      forEachMeshOf(hoverGuid, (mesh) => setMeshHoverTint(mesh, true));
      hoverVoxelInstancesOfGuid(hoverGuid, true);
    }
  }
}

// ゴースト表示（ux-spec.md §3.1）: 選択中、非選択のメッシュ要素を半透明にして
// 選択要素を相対的に浮かび上がらせる。共有マテリアルの罠（highlightMeshと同じ
// 問題）を避けるため、per-meshのプロパティ変更ではなく共有の単一ゴースト
// マテリアルへの差し替え+復元で実装する（clone乱発を避ける、ux-spec.md記載の
// 方針通り）。スコープはメッシュ表示のみ（ボクセルのInstancedMeshは
// インスタンス単位の不透明度を持たず、対応には別途per-instance alpha機構が
// 必要なため、このストーリーでは対象外とする）。
const _ghostMaterial = new THREE.MeshBasicMaterial({
  color: 0x888888,
  transparent: true,
  opacity: 0.15,
  depthWrite: false,
});
let ghostModeEnabled = false;

function _setMeshGhosted(mesh, ghosted) {
  if (ghosted) {
    if (mesh.userData.__preGhostMaterial === undefined) {
      mesh.userData.__preGhostMaterial = mesh.material;
    }
    mesh.material = _ghostMaterial;
  } else if (mesh.userData.__preGhostMaterial !== undefined) {
    mesh.material = mesh.userData.__preGhostMaterial;
    delete mesh.userData.__preGhostMaterial;
    // ゴースト中にワイヤフレームがトグルされても、setWireframeEnabledはその時点の
    // mesh.material(共有の_ghostMaterial)にしか触れず、__preGhostMaterial(元の
    // マテリアル)側は更新されない。復元時に現在のwireframeEnabledへ同期する
    // (PRレビュー指摘: ゴーストOFFでwireframe状態が食い違う不整合を修正)。
    mesh.material.wireframe = wireframeEnabled;
  }
}

function _applyGhostState() {
  for (const [guid, obj] of objectsByGuid) {
    const shouldGhost = ghostModeEnabled && selectedGuid !== null && guid !== selectedGuid;
    obj.traverse((child) => {
      if (child.isMesh && child.material && child.name !== "__outline") _setMeshGhosted(child, shouldGhost);
    });
  }
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
  // Texture defaults to flipY=true (canvas row 0 -> texture v=1), but PlaneGeometry
  // also puts v=1 at its local +Y (high world Y / iy_max) edge. Combined, that would
  // sample canvas row 0 (values[0], i.e. iy_min) at the iy_max edge - a vertical
  // mirror of the data. flipY=false makes canvas row 0 map to v=0 (iy_min edge, the
  // low-Y side), matching how values[row] was built (row 0 = iy_min).
  texture.flipY = false;
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

// --- Live: ビルOS連携デジタルツイン表示 (E9-4, digital-twin-spec.md §5.1-§5.3) ---

// turbo系カラーマップの多項式近似 (Anton Mikhailov / Google Research, 2019, Apache-2.0
// "Turbo, An Improved Rainbow Colormap for Visualization"を移植)。外部ファイル・
// CDN依存なしで256エントリのLUTを事前計算する（build_twin_json()のcolormap名
// "turbo"に対応する唯一の実装、E9-4の受け入れ条件）。
function _turboColor(x) {
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

const TURBO_LUT = Array.from({ length: 256 }, (_, i) => _turboColor(i / 255));

// 凡例のグラデーションバーはCSS linear-gradientで描く（<canvas>にしない）。
// <canvas>にすると、既存の全PlaywrightテストがWebGL描画結果の画素検証に使う
// `document.querySelector('#viewport canvas')`（唯一のcanvas要素という前提）が、
// レンダラーのcanvas(viewport.appendChild(renderer.domElement)でJS実行時に
// 追加され、DOM順で常にこの凡例より後になる)より先にこちらへマッチしてしまい、
// 3D描画ではなく凡例バーの画素を検証してしまう（実際に踏んだ回帰）。
const TURBO_GRADIENT_CSS = (() => {
  const stops = 16;
  const parts = [];
  for (let i = 0; i <= stops; i++) {
    const t = i / stops;
    const [r, g, b] = TURBO_LUT[Math.round(t * 255)];
    parts.push(`rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}) ${Math.round(t * 100)}%`);
  }
  return `linear-gradient(to right, ${parts.join(", ")})`;
})();

// twin.json（build_twin_json()の出力）。scene.jsonにassets.twinが無ければnullの
// まま（SDFスライスと同じ付加的アセット規約）。
let liveTwinConfig = null;
// guid -> [{pointId, metric, target}, ...]（liveTwinConfig.bindingsのtarget.guidで
// 引けるように整理したもの。プロパティパネルのLive Dataセクション用）。
const liveBindingsByGuid = new Map();
let liveEnabled = false;
let livePaused = false;
let liveMetric = null;
let livePollTimer = null;

async function loadTwin(twinUrl) {
  const response = await fetch(twinUrl);
  if (!response.ok) {
    throw new Error(`failed to load twin.json: ${response.status}`);
  }
  liveTwinConfig = await response.json();

  liveBindingsByGuid.clear();
  for (const binding of liveTwinConfig.bindings) {
    const guid = binding.target && binding.target.guid;
    if (!guid) continue; // spaceGuidバインディングはE9-5(空間ボクセルヒートマップ)のスコープ
    if (!liveBindingsByGuid.has(guid)) liveBindingsByGuid.set(guid, []);
    liveBindingsByGuid.get(guid).push(binding);
  }

  liveMetricSelect.innerHTML = "";
  for (const metric of liveTwinConfig.metrics) {
    const option = document.createElement("option");
    option.value = metric.name;
    option.textContent = metric.name;
    liveMetricSelect.appendChild(option);
  }
  liveMetric = liveTwinConfig.metrics.length > 0 ? liveTwinConfig.metrics[0].name : null;
  if (liveMetric) liveMetricSelect.value = liveMetric;

  liveToolbarGroup.style.display = "";
  liveToolbarDivider.style.display = "";
}

function _metricDefinition(name) {
  return liveTwinConfig.metrics.find((m) => m.name === name);
}

// gltf.py/usd.pyのマテリアル重複排除（_ensureOwnMaterial参照）と同じ理由で、
// 値の色を書き込む前にメッシュ専有のマテリアルを確保し、元の色は
// mesh.userData.__liveOriginalColorへ一度だけ退避する（ゴースト/ホバー等の
// 既存パターンと同じuserData退避方式）。
//
// ゴースト中のメッシュは`mesh.material`が全要素で共有される単一の
// `_ghostMaterial`を指す（_setMeshGhosted参照）。ここでガードせずに
// `.color`を書き換えると、ゴースト中の全要素の色がこの1要素の値色で
// 汚染されてしまう（selectByGuidがhighlightMesh適用前に
// `_setMeshGhosted(mesh, false)`で選択要素自身のゴーストを解除している
// のと同じ種類の罠）。ゴースト中は単に何もしない——ゴースト解除後の
// 次回ポーリングで正しく着色される。
function _setLiveColorForGuid(guid, rgb) {
  forEachMeshOf(guid, (mesh) => {
    if (mesh.material === _ghostMaterial) return;
    _ensureOwnMaterial(mesh);
    if (mesh.userData.__liveOriginalColor === undefined) {
      mesh.userData.__liveOriginalColor = mesh.material.color.clone();
    }
    mesh.material.color.setRGB(rgb[0], rgb[1], rgb[2]);
  });
}

function clearLiveColors() {
  for (const guid of liveBindingsByGuid.keys()) {
    forEachMeshOf(guid, (mesh) => {
      if (mesh.material === _ghostMaterial) return;
      if (mesh.userData.__liveOriginalColor !== undefined) {
        mesh.material.color.copy(mesh.userData.__liveOriginalColor);
      }
    });
  }
}

function _formatLiveNumber(n) {
  return Number.isFinite(n) ? n.toFixed(1) : String(n);
}

function updateLiveLegend(metricDef, min, max) {
  liveLegendGradientEl.style.background = TURBO_GRADIENT_CSS;
  liveLegendMinLabel.textContent = _formatLiveNumber(min);
  liveLegendMaxLabel.textContent = _formatLiveNumber(max);
  liveLegendUnitLabel.textContent = (metricDef && metricDef.unit) || "";
  // index.htmlの`#live-legend`はCSSルール側で`display: none`を持つ（インライン
  // styleではない）ため、`.style.display = ""`はそれを覆せない
  // （liveToolbarGroup/liveToolbarDividerはインラインstyleでdisplay:noneを
  // 持つため空文字列に戻すだけでよいが、こちらは明示的な値が必要）。
  liveLegend.style.display = "block";
}

// digital-twin-spec.md §5.2: 「動いているように見えて実は止まっている」事故を
// 防ぐため、datetimeがポーリング間隔×3(既定のstaleThresholdSeconds)より古い値は
// 彩度を落とした灰色寄りに描画する。
function _desaturateIfStale(rgb, datetime, nowMs, staleThresholdMs) {
  const isStale = nowMs - Date.parse(datetime) > staleThresholdMs;
  if (!isStale) return rgb;
  const gray = (rgb[0] + rgb[1] + rgb[2]) / 3;
  const amount = 0.7;
  return rgb.map((c) => c + (gray - c) * amount);
}

function _valueToLiveColor(value, min, max, datetime, nowMs, staleThresholdMs) {
  const t = (value - min) / (max - min);
  const lutIndex = Math.round(Math.min(1, Math.max(0, t)) * 255);
  return _desaturateIfStale(TURBO_LUT[lutIndex], datetime, nowMs, staleThresholdMs);
}

// 値の束を色へ変換して要素へ適用する共通処理。digital-twin-spec.md §5.5
// 「色適用関数はE9-4と共通化し、ライブ/再生で表示経路を分岐させない」に対応する
// 唯一の実装——E9-6(時系列再生)はポーリングではなくここへ履歴フレームの値と
// その時点の`nowMs`(再生ヘッドの時刻。実時刻ではない)を渡して呼ぶだけになる
// ように、fetch/setIntervalなどポーリング固有の処理は一切含めない。
function applyColorMappedValues(metric, values, minOverride, maxOverride, nowMs) {
  const metricDef = _metricDefinition(metric);
  let min = minOverride;
  let max = maxOverride;
  if (min === undefined || min === null || max === undefined || max === null) {
    // digital-twin-spec.md §5.2: min/max未指定なら受信値のP5〜P95で自動決定する。
    // NaN/Infinityは`typeof`ではnumberだが順序比較が意味を持たないため、
    // Number.isFinite()で明示的に除外する(バックエンド由来の異常値で
    // min/maxそのものがNaNになりLUT参照が壊れるのを防ぐ)。
    const nums = values
      .map((v) => v.value)
      .filter((v) => typeof v === "number" && Number.isFinite(v))
      .sort((a, b) => a - b);
    const percentile = (q) => nums[Math.min(nums.length - 1, Math.floor(q * (nums.length - 1)))];
    min = nums.length > 0 ? percentile(0.05) : 0;
    max = nums.length > 0 ? percentile(0.95) : 1;
  }
  if (max === min) max = min + 1; // ゼロ除算回避（全点が同一値の場合）

  const staleThresholdMs = (liveTwinConfig.staleThresholdSeconds ?? 30) * 1000;

  for (const entry of values) {
    if (!entry.guid) continue; // spaceGuidの集計表示はE9-5のスコープ
    const rgb = _valueToLiveColor(entry.value, min, max, entry.datetime, nowMs, staleThresholdMs);
    _setLiveColorForGuid(entry.guid, rgb);
  }

  updateLiveLegend(metricDef, min, max);
}

function applyLiveValues(body) {
  const metricDef = _metricDefinition(body.metric);
  applyColorMappedValues(
    body.metric,
    body.values,
    metricDef && metricDef.min,
    metricDef && metricDef.max,
    Date.now(),
  );
}

const _LIVE_TOGGLE_DEFAULT_TITLE = liveToggle.title;

async function refreshLiveValues() {
  if (!liveMetric) return;
  try {
    const response = await fetch(`./api/twin/values?metric=${encodeURIComponent(liveMetric)}`);
    if (!response.ok) throw new Error(`failed to fetch live values: ${response.status}`);
    applyLiveValues(await response.json());
    liveToggle.title = _LIVE_TOGGLE_DEFAULT_TITLE;
  } catch (error) {
    // ポーリングが繰り返し失敗しても、チェックボックス自体はON表示のまま
    // (盤面は最後に成功した値のまま静止する)なので、consoleを見ない限り
    // 気付けない「動いているように見えて実は止まっている」状態になりうる
    // (digital-twin-spec.md §5.2と同種の事故)。せめてtitleで手掛かりを残す。
    console.warn("ifc2usd viewer: failed to refresh live values", error);
    liveToggle.title = `${_LIVE_TOGGLE_DEFAULT_TITLE} (last refresh failed — see console)`;
  }
}

function stopLivePolling() {
  if (livePollTimer !== null) {
    clearInterval(livePollTimer);
    livePollTimer = null;
  }
}

function startLivePolling() {
  refreshLiveValues();
  const intervalMs = (liveTwinConfig.pollIntervalSeconds || 10) * 1000;
  livePollTimer = setInterval(() => {
    if (!livePaused) refreshLiveValues();
  }, intervalMs);
}

liveMetricSelect.addEventListener("change", () => {
  liveMetric = liveMetricSelect.value;
  if (liveEnabled) refreshLiveValues();
});

liveToggle.addEventListener("change", () => {
  liveEnabled = liveToggle.checked;
  livePauseToggle.disabled = !liveEnabled;
  if (liveEnabled) {
    startLivePolling();
  } else {
    stopLivePolling();
    clearLiveColors();
    liveLegend.style.display = "none";
  }
});

livePauseToggle.addEventListener("change", () => {
  livePaused = livePauseToggle.checked;
  if (!livePaused && liveEnabled) refreshLiveValues();
});

function _drawSparkline(canvas, history) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (history.length < 2) return;

  const values = history.map((h) => h.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;

  ctx.strokeStyle = _cssVar("--accent", "#3355ff");
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  history.forEach((point, i) => {
    const x = (i / (history.length - 1)) * canvas.width;
    const y = canvas.height - ((point.value - min) / range) * canvas.height;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function _populateLiveDataRow(pointId, valueEl, canvas) {
  try {
    const end = new Date();
    const start = new Date(end.getTime() - 60 * 60 * 1000); // 直近1時間
    const url =
      `./api/twin/history?pointId=${encodeURIComponent(pointId)}` +
      `&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}` +
      "&granularity=None";
    const response = await fetch(url);
    if (!response.ok) throw new Error(`failed to fetch history: ${response.status}`);
    const history = await response.json();
    _drawSparkline(canvas, history);
    const last = history[history.length - 1];
    valueEl.textContent = last ? `${last.value} @ ${last.datetime}` : "no data";
  } catch (error) {
    console.warn("ifc2usd viewer: failed to load live data history", error);
    valueEl.textContent = "unavailable";
  }
}

// #property-live-data（renderPropertyPanelがプレースホルダとして確保する
// 空div、E8-4由来）の中身をこのGUIDに紐づくポイントで埋める。twin.jsonが
// 無い/このGUIDに紐づくポイントが無い場合は空のままにする。
function _updateLiveDataSection(guid) {
  const container = document.getElementById("property-live-data");
  if (!container) return;
  container.innerHTML = "";
  if (!liveTwinConfig) return;

  const bindings = liveBindingsByGuid.get(guid) || [];
  if (bindings.length === 0) return;

  const heading = document.createElement("h3");
  heading.textContent = "Live Data";
  container.appendChild(heading);

  for (const binding of bindings) {
    const row = document.createElement("div");
    row.className = "property-live-data-row";

    const metricDef = _metricDefinition(binding.metric);
    const label = document.createElement("div");
    label.className = "property-live-data-label";
    label.textContent = metricDef ? `${binding.metric} (${metricDef.unit})` : binding.metric;
    row.appendChild(label);

    const valueEl = document.createElement("div");
    valueEl.className = "property-live-data-value";
    valueEl.textContent = "…";
    row.appendChild(valueEl);

    const canvas = document.createElement("canvas");
    canvas.className = "property-live-data-sparkline";
    canvas.width = 260;
    canvas.height = 40;
    row.appendChild(canvas);

    container.appendChild(row);
    _populateLiveDataRow(binding.pointId, valueEl, canvas);
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

// voxels.json v3の`indices`はdelta+RLE符号化された{base, deltas}形式（Issue #38 /
// E7-4、ifc2usd.voxel.encode_morton_indicesと対）。素朴な配列（v2互換ファイルや
// convertV1VoxelJsonの出力）はそのまま返し、両形式を透過的に扱う。
function decodeMortonIndices(indices) {
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
    const elementCodes = lod.elements.map((el) => decodeMortonIndices(el.indices));
    const totalInstances = elementCodes.reduce((sum, codes) => sum + codes.length, 0);

    // vertexColors:trueは指定しない: three.jsはInstancedMesh.instanceColorによる
    // per-instance色(USE_INSTANCING_COLOR)をmaterial.vertexColorsの値と無関係に
    // 有効化する一方、vertexColors:trueは*別に*ジオメトリ側のper-vertex color
    // 属性(USE_COLOR)も要求してしまう。_voxelUnitBoxにはcolor属性が無いため、
    // 未バインドのattributeがWebGLの既定値(0,0,0,1)を返し、vColorがinstanceColor
    // 乗算前に(0,0,0)へゼロ化されボクセルが常に真っ黒になっていた
    // （Issue #39 / E8-6。最小再現でVertexAttribPointer/シェーダ定義まで確認済み）。
    const material = new THREE.MeshStandardMaterial({ wireframe: wireframeEnabled });
    const mesh = new THREE.InstancedMesh(_voxelUnitBox, material, totalInstances);
    const instanceGuids = new Array(totalInstances);

    let instanceIndex = 0;
    lod.elements.forEach((el, elIndex) => {
      color.setRGB(el.color[0], el.color[1], el.color[2]);
      for (const code of elementCodes[elIndex]) {
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
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    // 初期visibleは常にfalseにしておき、実際の可視状態はapplyDisplayState()に
    // 一元化する（表示モード/アクティブLODの決定ロジックを1箇所にまとめるため）。
    mesh.visible = false;

    // 選択ハイライト(highlightVoxelInstancesOfGuid)が変更前の色へ復元できるよう、
    // 元のper-instance色をここでコピーして保持しておく（instanceColor.array自体は
    // ハイライト表示中に上書きされるため、生きた参照ではなく複製が必要）。
    const originalColors = mesh.instanceColor ? mesh.instanceColor.array.slice() : null;

    // guid -> そのguidが占めるinstance索引の配列。ハイライト時に全instanceGuidsを
    // 線形走査せずに済むよう、構築時に一度だけ索引化しておく
    // （objectsByGuid/nodesByGuidと同じ「guidキーのMapを1箇所で持つ」パターン）。
    const instanceIndicesByGuid = new Map();
    for (let i = 0; i < instanceGuids.length; i++) {
      const g = instanceGuids[i];
      let list = instanceIndicesByGuid.get(g);
      if (!list) {
        list = [];
        instanceIndicesByGuid.set(g, list);
      }
      list.push(i);
    }

    voxelRoot.add(mesh);
    voxelLods.push({ size, mesh, instanceGuids, originalColors, instanceIndicesByGuid });

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

// ワイヤフレーム表示。表示モード(mesh/voxel/both)とは直交する切替で、
// メッシュ・ボクセルの両マテリアルへ同時に効かせる。非表示のLOD/glbRootにも
// 適用しておく(表示モード切替時に改めて設定し直す必要が無いよう、材質側の
// 状態として持たせる)。
let wireframeEnabled = wireframeToggle.checked;

function setWireframeEnabled(enabled) {
  wireframeEnabled = enabled;
  if (glbRoot) {
    glbRoot.traverse((child) => {
      if (child.isMesh && child.material) child.material.wireframe = enabled;
    });
  }
  for (const lod of voxelLods) {
    lod.mesh.material.wireframe = enabled;
  }
}

wireframeToggle.addEventListener("change", () => setWireframeEnabled(wireframeToggle.checked));

ghostToggle.addEventListener("change", () => {
  ghostModeEnabled = ghostToggle.checked;
  _applyGhostState();
});

// パネル開閉(E8-5)。左右パネルの幅をCSSで0にするだけで、#viewportは
// flex:1のため自動的に広がる。既存のResizeObserver(#viewportを監視)が
// レイアウト確定後に発火してcamera.aspect/renderer.setSizeを追従させる
// ため、ここで#viewportのサイズを直接計算する必要はない。
treePanelToggle.addEventListener("click", () => {
  const collapsed = treePanel.classList.toggle("collapsed");
  treePanelToggle.textContent = collapsed ? "›" : "‹";
});

propertyPanelToggle.addEventListener("click", () => {
  const collapsed = propertyPanel.classList.toggle("collapsed");
  propertyPanelToggle.textContent = collapsed ? "‹" : "›";
});

// キーボードショートカット(E8-5)。input/textarea/contentEditableへ
// フォーカス中は無効にする(検索ボックス等での通常のタイピングを妨げないため)。
function _isTypingTarget(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

function _setDisplayModeViaShortcut(mode) {
  const input = document.querySelector(`input[name="display-mode"][value="${mode}"]`);
  if (input) input.checked = true;
  setDisplayMode(mode);
}

function toggleShortcutsOverlay(force) {
  const shouldShow = force !== undefined ? force : !shortcutsOverlay.classList.contains("visible");
  shortcutsOverlay.classList.toggle("visible", shouldShow);
}

window.addEventListener("keydown", (event) => {
  if (_isTypingTarget(document.activeElement)) return;
  // 修飾キー付きは常にブラウザ/OS側のショートカット（Ctrl+F検索、Ctrl+Wタブを閉じる等)
  // に譲る。ここで奪うと該当キーが常時使用不能になる(Copilotレビュー指摘、PR #48)。
  if (event.ctrlKey || event.metaKey || event.altKey) return;

  switch (event.key) {
    case "f":
    case "F": {
      const box = selectedGuid !== null ? getBoundingBoxOfGuid(selectedGuid) : modelBoundingBox;
      fitCameraToBox(box);
      break;
    }
    case "Escape":
      // ショートカット一覧が開いていればそちらを優先して閉じる(モーダル的挙動)。
      if (shortcutsOverlay.classList.contains("visible")) {
        toggleShortcutsOverlay(false);
      } else {
        selectByGuid(null);
      }
      break;
    case "w":
    case "W":
      wireframeToggle.checked = !wireframeToggle.checked;
      setWireframeEnabled(wireframeToggle.checked);
      break;
    case "1":
      _setDisplayModeViaShortcut("mesh");
      break;
    case "2":
      _setDisplayModeViaShortcut("voxel");
      break;
    case "3":
      _setDisplayModeViaShortcut("both");
      break;
    case "?":
      toggleShortcutsOverlay();
      break;
    default:
      return; // 対象外のキーはpreventDefaultしない
  }
  event.preventDefault();
});

// v1（ノートブックGLTF_to_Voxel.ipynb形式）→v2変換（spec.md §2の後方互換規定、
// Issue #17 / E1-5）。Python側のifc2usd.voxel.convert_v1_voxel_jsonと同一の変換
// （そちらのdocstringに変換規則の詳細）。ビューワー読み込み時にも受け付ける
// ことで、旧ノートブックの既存出力を手動配置したディレクトリでもそのまま
// 表示できる。
function convertV1VoxelJson(v1) {
  const size = v1.voxelSize;
  const origin = v1.offset.map((component) => component * size);
  const elements = (v1.elements || []).map((el) => {
    const [r, g, b] = mortonDecode(el.color);
    return {
      guid: el.guid,
      class: el.class,
      name: el.name ?? null,
      color: [r / 255, g / 255, b / 255],
      indices: [...el.indices].sort((a, b2) => a - b2),
    };
  });
  return {
    version: 2,
    units: "m",
    upAxis: "Z",
    source: { convertedFrom: "v1" },
    origin,
    lods: [{ size, elements }],
  };
}

async function loadVoxels(voxelsUrl) {
  const response = await fetch(voxelsUrl);
  if (!response.ok) {
    throw new Error(`failed to load voxels: ${response.status}`);
  }
  let voxelDescription = await response.json();
  if (voxelDescription.voxelSize !== undefined && voxelDescription.offset !== undefined) {
    voxelDescription = convertV1VoxelJson(voxelDescription);
  }
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

function _guidAtClientPosition(clientX, clientY) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointerNdc.x = ((clientX - rect.left) / rect.width) * 2 - 1;
  pointerNdc.y = -((clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(pointerNdc, camera);
  // 既知の制約（Issue #18時点で未対応、意図的に据え置き）: three.jsのRaycasterは
  // renderer.clippingPlanesを考慮しないため、断面クリップで視覚的に隠れている
  // ジオメトリも普通にクリックで選択できてしまう。FR-10自体は表示上の断面機能で
  // あり選択への影響は要件外のため、このissueでは対応しない。必要になれば
  // ここで `intersections.filter(i => i.point.y <= sectionClipPlane.constant)` の
  // ようなフィルタを追加する。
  const intersections = raycaster.intersectObjects(currentRaycastTargets(), true);
  if (intersections.length === 0) return null;

  const hit = intersections[0];
  return hit.object.isInstancedMesh
    ? findGuidOfVoxelInstance(hit.object, hit.instanceId)
    : findGuidOfObject(hit.object);
}

renderer.domElement.addEventListener("pointerup", (event) => {
  const downPosition = pointerDownPosition;
  pointerDownPosition = null;
  if (!downPosition || !event.isPrimary || event.button !== 0) return;

  const dx = event.clientX - downPosition.x;
  const dy = event.clientY - downPosition.y;
  if (Math.hypot(dx, dy) > CLICK_DRAG_THRESHOLD_PX) return;

  const guid = _guidAtClientPosition(event.clientX, event.clientY);
  if (guid !== null) selectByGuid(guid);
});

// ダブルクリックで選択+フィット(ux-spec.md §3.1)。ブラウザはdblclick発火前に
// 通常のclick/pointerup2回をそのまま発火させるため、1回目のクリックで既に選択済み
// になった上でフィットが追加される（selectByGuidの2回目呼び出しは同一guidなら
// no-opなので無害）。
renderer.domElement.addEventListener("dblclick", (event) => {
  const guid = _guidAtClientPosition(event.clientX, event.clientY);
  if (guid === null) return;
  selectByGuid(guid);
  fitCameraToBox(getBoundingBoxOfGuid(guid));
});

// 3D側のホバー(E8-2)。pointermoveはドラッグ中も含め高頻度に発火するため、
// ここではレイキャストせず「最後のポインタ位置」を記録するだけにし、実際の
// レイキャストはanimate()のrAFループ内で毎フレーム高々1回だけ行う
// （受け入れ条件: レイキャストは1フレーム1回以内）。event.buttons!==0は
// いずれかのボタンが押下中(=OrbitControlsのドラッグ操作中)を意味するため、
// その間はホバー更新自体をスキップする（受け入れ条件: ドラッグ中はスキップ）。
let _pendingHoverClientPosition = null;
let hoverRaycastCount = 0; // E2Eテスト用(1フレーム1回以内であることの検証)

renderer.domElement.addEventListener("pointermove", (event) => {
  if (event.buttons !== 0) return;
  _pendingHoverClientPosition = { x: event.clientX, y: event.clientY };
});

renderer.domElement.addEventListener("pointerleave", () => {
  _pendingHoverClientPosition = null;
  renderer.domElement.style.cursor = "";
  setHoverGuid(null);
});

function _processPendingHover() {
  if (!_pendingHoverClientPosition) return;
  const { x, y } = _pendingHoverClientPosition;
  _pendingHoverClientPosition = null;
  hoverRaycastCount++;
  const guid = _guidAtClientPosition(x, y);
  // ux-spec.md §3.2: 3D側のホバーでカーソルをpointerにする。ツリー行は既存の
  // CSS(.tree-label { cursor: pointer })で常にpointerなので対象外。
  renderer.domElement.style.cursor = guid !== null ? "pointer" : "";
  setHoverGuid(guid);
}

// 初期展開状態(E8-3 / ux-spec.md §3.3): Storey(IfcBuildingStorey)までは展開し、
// それより下(要素)は畳む。
function _defaultExpandedForClass(cls) {
  return cls === "IfcSite" || cls === "IfcBuilding" || cls === "IfcBuildingStorey";
}

function _rowPartOf(li, selector) {
  return li.querySelector(`:scope > .tree-row > ${selector}`);
}

function _setNodeExpanded(guid, expanded) {
  const li = _liByGuid.get(guid);
  if (!li) return;
  const toggle = _rowPartOf(li, ".tree-toggle");
  const ul = li.querySelector(":scope > ul");
  if (toggle && !toggle.classList.contains("tree-toggle-empty")) {
    toggle.textContent = expanded ? "▾" : "▸";
  }
  if (ul) ul.style.display = expanded ? "" : "none";
}

// guidの祖先(親→…→ルート)をすべて展開する。選択時の自動展開・検索での
// マッチ行表示の両方から使う共通ロジック。
function _expandAncestorsOf(guid) {
  let parent = _parentGuidByGuid.get(guid);
  while (parent !== undefined && parent !== null) {
    _setNodeExpanded(parent, true);
    parent = _parentGuidByGuid.get(parent);
  }
}

function _isDescendantOrSelf(guid, ancestorGuid) {
  let current = guid;
  while (current !== undefined && current !== null) {
    if (current === ancestorGuid) return true;
    current = _parentGuidByGuid.get(current);
  }
  return false;
}

// isolate(E8-3): 対象サブツリー以外を非表示にする。同じ行をもう一度押すと解除する。
// 一度に有効化できるisolateは1つ(スコープを絞ったシンプルな実装)。
// スコープ外(既知の制約、ゴーストモードがボクセルを対象外にしているのと同じ理由):
// - ボクセル(voxelRoot配下のInstancedMesh)はobjectsByGuid/setObjectVisibleの対象外
//   のため、isolateはメッシュ表示にのみ効く。
// - isolate ON/OFF時、各行の可視性チェックボックスを一括で書き換えるため、
//   isolate中に手動でチェックボックスを操作していた場合、その変更はisolate
//   解除時に失われる(全要素可視へ戻る)。可視性の事前状態を保存/復元するには
//   スナップショット機構が要るが、このストーリーの受け入れ条件には含まれない
//   ため見送る。
let isolatedGuid = null;

function toggleIsolate(guid) {
  const wasIsolating = isolatedGuid !== null;
  const target = isolatedGuid === guid ? null : guid;

  if (wasIsolating) {
    const prevLi = _liByGuid.get(isolatedGuid);
    if (prevLi) prevLi.classList.remove("isolated");
  }
  isolatedGuid = target;

  for (const [g, li] of _liByGuid) {
    // 対象の子孫だけでなく祖先(Site/Building/Storeyなど)も可視のままにする。
    // gltf.pyは空間階層の非メッシュノード(Site/Building/Storey)もThree.jsの
    // 親Object3D/Groupとして書き出しており、three.jsは親.visible===falseで
    // 子孫の描画自体を打ち切る(祖先を隠すと対象自身も画面から消えてしまう)。
    const visible = target === null || _isDescendantOrSelf(g, target) || _isDescendantOrSelf(target, g);
    setObjectVisible(g, visible);
    const checkbox = _rowPartOf(li, ".tree-visibility");
    if (checkbox) checkbox.checked = visible;
  }

  if (target !== null) {
    const li = _liByGuid.get(target);
    if (li) li.classList.add("isolated");
  }
}

function renderTreeNode(node) {
  const li = document.createElement("li");
  li.dataset.guid = node.guid;
  _liByGuid.set(node.guid, li);

  const row = document.createElement("div");
  row.className = "tree-row";

  const hasChildren = node.children && node.children.length > 0;
  const toggle = document.createElement("span");
  toggle.className = hasChildren ? "tree-toggle" : "tree-toggle tree-toggle-empty";
  if (hasChildren) {
    const expanded = _defaultExpandedForClass(node.class);
    toggle.textContent = expanded ? "▾" : "▸";
    toggle.addEventListener("click", () => {
      _setNodeExpanded(node.guid, toggle.textContent !== "▾");
    });
  }
  row.appendChild(toggle);

  const visibility = document.createElement("input");
  visibility.type = "checkbox";
  visibility.className = "tree-visibility";
  visibility.checked = true;
  visibility.addEventListener("change", () => setObjectVisible(node.guid, visibility.checked));
  row.appendChild(visibility);

  if (node.color) {
    const [r, g, b] = node.color;
    const chip = document.createElement("span");
    chip.className = "tree-color-chip";
    chip.style.background = `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
    row.appendChild(chip);
  }

  const label = document.createElement("span");
  label.className = "tree-label";
  label.textContent = node.name ? `${node.name} (${node.class})` : node.class;
  label.addEventListener("click", () => selectByGuid(node.guid));
  label.addEventListener("dblclick", () => {
    selectByGuid(node.guid);
    fitCameraToBox(getBoundingBoxOfGuid(node.guid));
  });
  // ホバーではスクロールしない(選択時のみスクロールする、E8-3と区別する)。
  label.addEventListener("mouseenter", () => setHoverGuid(node.guid));
  label.addEventListener("mouseleave", () => {
    if (hoverGuid === node.guid) setHoverGuid(null);
  });
  row.appendChild(label);

  const isolateBtn = document.createElement("button");
  isolateBtn.type = "button";
  isolateBtn.className = "tree-isolate-btn";
  isolateBtn.title = "Isolate";
  isolateBtn.textContent = "⊙";
  isolateBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleIsolate(node.guid);
  });
  row.appendChild(isolateBtn);

  li.appendChild(row);

  if (hasChildren) {
    const ul = document.createElement("ul");
    for (const child of node.children) {
      ul.appendChild(renderTreeNode(child));
    }
    if (!_defaultExpandedForClass(node.class)) ul.style.display = "none";
    li.appendChild(ul);
  }

  return li;
}

function renderTree(tree) {
  _liByGuid.clear();
  treeNodesContainer.innerHTML = "";
  const ul = document.createElement("ul");
  ul.className = "tree-root";
  for (const node of tree) {
    ul.appendChild(renderTreeNode(node));
  }
  treeNodesContainer.appendChild(ul);
}

// 検索/絞り込み(E8-3): 名前/クラス/GUIDの部分一致(大文字小文字無視)。
// マッチ行とその祖先だけを表示し、マッチ部分をハイライトする。
function _nodeSearchText(node) {
  return `${node.name || ""} ${node.class || ""} ${node.guid || ""}`.toLowerCase();
}

function _clearMatchHighlight(li) {
  const label = _rowPartOf(li, ".tree-label");
  if (!label || label.dataset.originalText === undefined) return;
  label.textContent = label.dataset.originalText;
}

function _highlightMatch(li, query) {
  const label = _rowPartOf(li, ".tree-label");
  if (!label) return;
  const original = label.dataset.originalText !== undefined ? label.dataset.originalText : label.textContent;
  label.dataset.originalText = original;

  const idx = original.toLowerCase().indexOf(query);
  if (idx === -1) {
    label.textContent = original;
    return;
  }
  label.textContent = "";
  label.appendChild(document.createTextNode(original.slice(0, idx)));
  const mark = document.createElement("mark");
  mark.className = "tree-match";
  mark.textContent = original.slice(idx, idx + query.length);
  label.appendChild(mark);
  label.appendChild(document.createTextNode(original.slice(idx + query.length)));
}

function applyTreeSearch(rawQuery) {
  const query = rawQuery.trim().toLowerCase();

  if (!query) {
    // クリアで全行の表示は戻すが、検索中にマッチ行を見せるため強制展開した
    // 祖先は畳み直さない(単なる絞り込み解除であり、折りたたみ状態のリセットは
    // 別の操作という整理。副作用として無害なため、この単純化を受け入れる)。
    for (const li of _liByGuid.values()) {
      li.classList.remove("search-hidden");
      _clearMatchHighlight(li);
    }
    return;
  }

  const matchedGuids = new Set();
  for (const [guid, node] of nodesByGuid) {
    if (_nodeSearchText(node).includes(query)) matchedGuids.add(guid);
  }

  const visibleGuids = new Set(matchedGuids);
  for (const guid of matchedGuids) {
    let parent = _parentGuidByGuid.get(guid);
    while (parent !== undefined && parent !== null) {
      visibleGuids.add(parent);
      parent = _parentGuidByGuid.get(parent);
    }
  }

  for (const [guid, li] of _liByGuid) {
    li.classList.toggle("search-hidden", !visibleGuids.has(guid));
    if (matchedGuids.has(guid)) {
      _highlightMatch(li, query);
      _expandAncestorsOf(guid); // 折りたたまれた祖先の中に埋もれないようにする
    } else {
      _clearMatchHighlight(li);
    }
  }
}

let _treeSearchDebounceTimer = null;
treeSearchInput.addEventListener("input", () => {
  clearTimeout(_treeSearchDebounceTimer);
  const value = treeSearchInput.value;
  _treeSearchDebounceTimer = setTimeout(() => applyTreeSearch(value), 150);
});

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
  if (wireframeEnabled) setWireframeEnabled(true);

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

  if (sceneDescription.assets.twin) {
    // twinもボクセル/sdf同様、無ければメッシュ表示自体には影響しない付加情報
    // (serve --twin未指定時の「既存ビューワー機能が完全に無変化で動く」という
    // E9のオフライン劣化要件)。
    try {
      await loadTwin(sceneDescription.assets.twin);
    } catch (error) {
      console.warn("ifc2usd viewer: failed to load twin.json, continuing without live mode", error);
    }
  }

  return sceneDescription;
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  updateClipPlanes();
  _processPendingHover();
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
  decodeMortonIndices,
  getGlbRoot: () => glbRoot,
  setDisplayMode,
  getDisplayMode: () => displayMode,
  setActiveVoxelLodIndex,
  currentRaycastTargets,
  getSectionClipHeight: () => sectionClipPlane.constant,
  setSectionClipHeight,
  hasSdfSlicesFor: (guid) => sdfSlicesByGuid.has(guid),
  getSdfSliceMesh: () => sdfSliceMesh,
  getLiveTwinConfig: () => liveTwinConfig,
  isLiveEnabled: () => liveEnabled,
  getLiveOriginalColor: (guid) => {
    let color = null;
    forEachMeshOf(guid, (mesh) => {
      if (color === null) color = mesh.userData.__liveOriginalColor ?? null;
    });
    return color;
  },
  setGhostModeEnabled: (enabled) => {
    ghostModeEnabled = enabled;
    _applyGhostState();
  },
  getGhostModeEnabled: () => ghostModeEnabled,
  isMeshGhosted: (mesh) => mesh.material === _ghostMaterial,
  getHoverGuid: () => hoverGuid,
  setHoverGuid,
  getHoverRaycastCount: () => hoverRaycastCount,
  applyTreeSearch,
  toggleIsolate,
  getIsolatedGuid: () => isolatedGuid,
};

resize();
animate();
// 未選択時の操作ガイド(E8-4)はシーン読み込み完了を待たず、ページ表示直後から
// 見えているべき状態。
renderPropertyPanel(null);

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
