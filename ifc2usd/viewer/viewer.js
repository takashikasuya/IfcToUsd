// ifc2usd Web ビューワー。
// scene.json を読み込み、GLBの表示・カメラ操作を行う（階層ツリー・選択は
// E3-4/E3-5 で追加する）。

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const viewport = document.getElementById("viewport");

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x202020);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);

const renderer = new THREE.WebGLRenderer({ antialias: true });
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

  modelBoundingBox = new THREE.Box3().setFromObject(modelRoot);
  fitAll();

  return sceneDescription;
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  updateClipPlanes();
  renderer.render(scene, camera);
}

// Playwright/E2Eテスト、および後続issue（E3-4/E3-5）が使うフック。
window.ifc2usdViewer = {
  scene,
  camera,
  controls,
  renderer,
  modelRoot,
  fitCameraToBox,
  fitAll,
  getBoundingBox: () => modelBoundingBox,
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
