"""Live: 値の色マッピング + 凡例 + Live DataパネルのE2Eテスト（Issue #53 / E9-4）。

`mock_twin_server`（tests/conftest.py、digital-twin-spec.md §2のペイロード形を
返す）を上流に見立て、`ifc2usd serve --twin`相当の構成（`build_serve_directory`
に`twin`、`make_server`に`twin_proxy`）を実際に起動して検証する。GUID↔pointIdの
結合は`mapping.json`（bindings）が担う層なので、モック側は既存の`point-temp-1`
（温度、値23.4）をそのまま使い、bindingsのtarget.guidだけをこのテスト用に
変換済みUSDの実際の壁GUIDへ差し替える。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from pxr import Usd

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from ifc2usd.twin import TwinClient, build_twin_json
from ifc2usd.twin_proxy import TwinProxy
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _turbo_color(x: float) -> tuple[float, float, float]:
    """viewer.jsの_turboColorと同じ多項式近似のPython版
    （手計算による期待値のズレを避けるため、同じ式をそのまま実行して比較する）。"""
    x = min(1.0, max(0.0, x))
    x2 = x * x
    x3 = x2 * x
    x4 = x2 * x2
    x5 = x3 * x2
    r = 0.13572138 + 4.6153926 * x - 42.66032258 * x2 + 132.13108234 * x3 - 152.94239396 * x4 + 59.28637943 * x5
    g = 0.09140261 + 2.19418839 * x + 4.84296658 * x2 - 14.18503333 * x3 + 4.27729857 * x4 + 2.82956604 * x5
    b = 0.1066733 + 12.64194608 * x - 60.58204836 * x2 + 110.36276771 * x3 - 89.90310912 * x4 + 27.34824973 * x5
    return (min(1.0, max(0.0, r)), min(1.0, max(0.0, g)), min(1.0, max(0.0, b)))


def _wall_guid(usda_path: Path, name: str) -> str:
    stage = Usd.Stage.Open(str(usda_path))
    for prim in stage.Traverse():
        cd = prim.GetCustomData()
        if cd.get("class") == "IfcWall" and cd.get("Name") == name:
            return cd["GUID"]
    raise AssertionError(f"wall not found: {name}")


@pytest.fixture(scope="module")
def north_wall_guid(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("twin_live_e2e_guid")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)
    return _wall_guid(usda, "Wall North")


@pytest.fixture(scope="module")
def served_url_with_twin(tmp_path_factory, mock_twin_server, north_wall_guid):
    tmp_path = tmp_path_factory.mktemp("twin_live_e2e")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    bindings = [
        {"pointId": "point-temp-1", "metric": "temperature", "target": {"guid": north_wall_guid}}
    ]
    twin_json = build_twin_json(
        metrics=[{"name": "temperature", "unit": "celsius", "colormap": "turbo", "min": 0, "max": 23.4}],
        bindings=bindings,
        # ポーリング間隔・stale閾値は長めに: テスト中に余計なポーリングが走ったり、
        # モックの固定datetime("2026-07-08T09:00:00Z")が実行時刻との差でstale
        # 判定されてしまったりしないようにする。
        poll_interval_seconds=3600,
        stale_threshold_seconds=10**9,
    )

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir, twin=twin_json)

    client = TwinClient(mock_twin_server)
    proxy = TwinProxy(client, bindings, ttl_seconds=twin_json["pollIntervalSeconds"])

    server = make_server(workdir, port=0, twin_proxy=proxy)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def served_url_without_twin(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("twin_live_e2e_no_twin")
    usda = tmp_path / "minimal.usda"
    convert(FIXTURE, usda)

    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}/"

    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(**chromium_launch_kwargs())
        yield b
        b.close()


@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1000, "height": 700})
    yield p
    p.close()


def _wait_for_load(page, url, e2e: bool = True):
    page.goto(f"{url}?e2e" if e2e else url)
    page.wait_for_function("window.ifc2usdLoaded === true", timeout=10000)


def _guid_by_name(page, name):
    return page.evaluate(f"""
        () => {{
            let found = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {{
                if (obj.userData && obj.userData.name === {name!r}) found = obj.userData.guid;
            }});
            return found;
        }}
    """)


_CANDIDATE_VIEW_DIRECTIONS = [
    (5, 10, 7),
    (1, 1, 1),
    (1, 1, -1),
    (1, -1, 1),
    (-1, 1, 1),
    (1, 0, 0),
    (0, 1, 0),
    (0, 0, 1),
    (-1, -1, -1),
    (0, 1, 1),
]


def _sample_mesh_pixel_at_guid(page, guid):
    """test_viewer_e2e.py の_sample_pixel_at_guidと同じ理由（薄い壁は視点方向により
    自己遮蔽/逆光面しか見えないことがある）で複数方向を試し、最も彩度の高い画素を返す。"""
    page.evaluate('window.ifc2usdViewer.setDisplayMode("mesh")')
    best = (32, 32, 32)
    best_saturation = -1
    for dx, dy, dz in _CANDIDATE_VIEW_DIRECTIONS:
        page.evaluate(f"""
            () => {{
                window.ifc2usdViewer.camera.position.set({dx}, {dy}, {dz});
                window.ifc2usdViewer.controls.target.set(0, 0, 0);
                const box = window.ifc2usdViewer.getBoundingBoxOfGuid("{guid}");
                window.ifc2usdViewer.fitCameraToBox(box, {{ paddingFactor: 1.5 }});
            }}
        """)
        page.wait_for_timeout(100)
        r, g, b = page.evaluate("""
            () => {
                const canvas = document.querySelector('#viewport canvas');
                const tmp = document.createElement('canvas');
                tmp.width = canvas.width;
                tmp.height = canvas.height;
                const ctx = tmp.getContext('2d');
                ctx.drawImage(canvas, 0, 0);
                const x = Math.floor(tmp.width / 2);
                const y = Math.floor(tmp.height / 2);
                const [r, g, b] = ctx.getImageData(x, y, 1, 1).data;
                return [r, g, b];
            }
        """)
        saturation = max(r, g, b) - min(r, g, b)
        if saturation > best_saturation:
            best_saturation = saturation
            best = (r, g, b)
    return best


def test_live_toolbar_group_hidden_without_twin_asset(page, served_url_without_twin):
    _wait_for_load(page, served_url_without_twin)
    assert page.locator("#live-toolbar-group").is_visible() is False


def test_live_toolbar_group_visible_with_metric_options(page, served_url_with_twin):
    _wait_for_load(page, served_url_with_twin)
    assert page.locator("#live-toolbar-group").is_visible() is True
    options = page.locator("#live-metric-select option").all_inner_texts()
    assert options == ["temperature"]


def test_enabling_live_colors_bound_element_and_shows_legend(page, served_url_with_twin, north_wall_guid):
    """value(23.4)==max(23.4)としたのでt=1.0、turbo(1.0)は赤が支配的な色になる
    （手計算した期待値ではなくviewer.jsと同じ多項式をPython側で実行して比較する）。"""
    _wait_for_load(page, served_url_with_twin)

    assert page.locator("#live-legend").is_visible() is False

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)  # 非同期のrefreshLiveValues()完了待ち

    expected_r, expected_g, expected_b = _turbo_color(1.0)
    assert expected_r > expected_g and expected_r > expected_b, "test premise: t=1.0 should be red-dominant"

    r, g, b = _sample_mesh_pixel_at_guid(page, north_wall_guid)
    assert r > g and r > b, f"expected red-dominant live color, got rgb=({r},{g},{b})"

    assert page.locator("#live-legend").is_visible() is True
    assert page.locator("#live-legend-unit").inner_text() == "celsius"
    assert page.locator("#live-legend-max").inner_text() == "23.4"


def test_disabling_live_restores_original_color(page, served_url_with_twin, north_wall_guid):
    """Wall North自体はdisplayColor (0.8, 0.2, 0.2)の赤壁のため、
    "元の色に戻る"こと自体は単独では検証しにくい。ここでは
    getLiveOriginalColor()で退避済みの元色オブジェクトが実際にmesh.material.color
    へコピーされる（=Liveオフ後に__liveOriginalColorと一致する）ことを確認する。"""
    _wait_for_load(page, served_url_with_twin)

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    page.locator("#live-toggle").uncheck()

    matches = page.evaluate(f"""
        () => {{
            let ok = true;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {{
                if (obj.userData && obj.userData.guid === {north_wall_guid!r} && obj.isMesh) {{
                    const original = obj.userData.__liveOriginalColor;
                    if (original && !obj.material.color.equals(original)) ok = false;
                }}
            }});
            return ok;
        }}
    """)
    assert matches is True
    assert page.locator("#live-legend").is_visible() is False


def test_live_does_not_recolor_shared_ghost_material(page, served_url_with_twin, north_wall_guid):
    """コードレビューで検出: ゴースト中の要素は全て単一の共有マテリアル
    (_ghostMaterial)を指すため、ガード無しにLiveの色をそこへ書き込むと
    ゴースト中の全要素の色が汚染される。Wall Northをゴースト状態にしたまま
    Liveを有効化しても、共有ゴーストマテリアル自体の色は変化しないこと
    （=他の全ゴースト要素を巻き込まないこと）を確認する。"""
    _wait_for_load(page, served_url_with_twin)

    east_guid = _guid_by_name(page, "Wall East")
    page.evaluate(f"""
        () => {{
            window.ifc2usdViewer.setGhostModeEnabled(true);
            window.ifc2usdViewer.selectByGuid({east_guid!r});
        }}
    """)
    assert page.evaluate(f"window.ifc2usdViewer.getGhostModeEnabled()") is True

    ghost_color_before = page.evaluate("""
        () => {
            let color = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.isMesh && window.ifc2usdViewer.isMeshGhosted(obj) && color === null) {
                    color = [obj.material.color.r, obj.material.color.g, obj.material.color.b];
                }
            });
            return color;
        }
    """)
    assert ghost_color_before is not None, "test premise: Wall North should be ghosted (unselected)"

    page.locator("#live-toggle").check()
    page.wait_for_function("window.ifc2usdViewer.isLiveEnabled() === true")
    page.wait_for_timeout(300)

    ghost_color_after = page.evaluate("""
        () => {
            let color = null;
            window.ifc2usdViewer.modelRoot.traverse((obj) => {
                if (obj.isMesh && window.ifc2usdViewer.isMeshGhosted(obj) && color === null) {
                    color = [obj.material.color.r, obj.material.color.g, obj.material.color.b];
                }
            });
            return color;
        }
    """)
    assert ghost_color_after == ghost_color_before, (
        f"shared ghost material color changed: {ghost_color_before} -> {ghost_color_after}"
    )


def test_live_data_section_shows_bound_metric_and_sparkline(page, served_url_with_twin, north_wall_guid):
    _wait_for_load(page, served_url_with_twin)
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({north_wall_guid!r})")

    live_data = page.locator("#property-live-data")
    # h3は`#property-live-data h3 { text-transform: uppercase }`で大文字レンダリング
    # されるため、大文字小文字を無視して比較する(test_property_panel_e2e.pyの
    # dt比較と同じ理由)。
    assert live_data.locator("h3").inner_text().lower() == "live data"
    # .property-live-data-labelもtext-transform: uppercaseでレンダリングされる。
    assert "temperature" in live_data.locator(".property-live-data-label").inner_text().lower()

    value_text = live_data.locator(".property-live-data-value")
    value_text.wait_for()
    page.wait_for_function(
        "document.querySelector('#property-live-data .property-live-data-value').textContent !== '…'"
    )
    assert "23.4" in value_text.inner_text()

    assert live_data.locator(".property-live-data-sparkline").count() == 1


def test_live_data_section_empty_for_unbound_element(page, served_url_with_twin):
    _wait_for_load(page, served_url_with_twin)
    east_guid = _guid_by_name(page, "Wall East")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({east_guid!r})")

    assert page.locator("#property-live-data").inner_text() == ""
