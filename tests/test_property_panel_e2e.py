"""プロパティパネル改良のE2Eテスト（Issue #45 / E8-4）。"""

from __future__ import annotations

import threading
from pathlib import Path

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.util.shape_builder
import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs
from tests.generate_fixture import _add_wall

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


def _build_fixture_with_site_lat_lon(ifc_path: Path) -> None:
    """RefLatitude/RefLongitudeを持つIfcSiteのフィクスチャを作る。usd.pyの
    set_custom_dataは、IfcSiteである限りLatitude/Longitude customData自体は
    常に書き込む(RefLatitude/RefLongitude未設定なら空文字列になるだけ)ため、
    tests/fixtures/minimal.ifcでも実は空文字列としてキーは存在する。10進変換の
    実際の数値を検証するにはRefLatitude/RefLongitudeが実際に設定されたフィクス
    チャが必要なため、ここで別途構築する。壁を1枚だけ置くのは、ジオメトリが
    皆無だとtrimeshのGLBエクスポートが空シーンとして失敗するため（このテストの
    主眼はSiteのLatitude/Longitudeであり、壁の内容自体は無関係）。"""
    model = ifcopenshell.api.project.create_file(version="IFC4")
    project = ifcopenshell.api.root.create_entity(model, ifc_class="IfcProject", name="P")
    metre = ifcopenshell.api.unit.add_si_unit(model, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(model, units=[metre])
    context = ifcopenshell.api.context.add_context(model, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        model, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=context
    )
    site = ifcopenshell.api.root.create_entity(model, ifc_class="IfcSite", name="Fixture Site")
    site.RefLatitude = (35, 40, 44, 0)
    site.RefLongitude = (139, 45, 30, 0)
    building = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuilding", name="Fixture Building")
    storey = ifcopenshell.api.root.create_entity(model, ifc_class="IfcBuildingStorey", name="Ground Floor")

    ifcopenshell.api.aggregate.assign_object(model, products=[site], relating_object=project)
    ifcopenshell.api.aggregate.assign_object(model, products=[building], relating_object=site)
    ifcopenshell.api.aggregate.assign_object(model, products=[storey], relating_object=building)

    builder = ifcopenshell.util.shape_builder.ShapeBuilder(model)
    _add_wall(model, body, storey, builder, "Wall", (0.0, 0.0, 0.0), (5.0, 0.2, 3.0), (0.8, 0.2, 0.2))

    ifc_path.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(ifc_path))


def _serve_fixture(tmp_path, fixture_path, name):
    usda = tmp_path / f"{name}.usda"
    convert(fixture_path, usda)

    workdir = tmp_path / name
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}/"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("property_panel_e2e")
    server, thread, url = _serve_fixture(tmp_path, FIXTURE, "www")
    yield url
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def lat_lon_served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("property_panel_lat_lon_e2e")
    ifc_path = tmp_path / "lat_lon.ifc"
    _build_fixture_with_site_lat_lon(ifc_path)
    server, thread, url = _serve_fixture(tmp_path, ifc_path, "www_lat_lon")
    yield url
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


def _wait_for_load(page, url):
    page.goto(url)
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


def test_property_keys_shown_in_defined_order(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    # dtは(text-transform: uppercaseのCSSにより)大文字でレンダリングされるため、
    # inner_text()は表示上のテキストを返す。大文字小文字を無視して比較する。
    keys = [k.lower() for k in page.locator("#property-panel dl dt").all_inner_texts()]
    # GUID/class/Name/LongName/Description/Latitude/Longitude の定義順の部分列
    # (このフィクスチャに存在するキーのみ) になっていること。
    expected_order = ["guid", "class", "name", "longname", "description", "latitude", "longitude"]
    filtered_expected = [k for k in expected_order if k in keys]
    assert keys == filtered_expected
    assert "guid" in keys and "class" in keys and "name" in keys


def test_class_shown_as_chip_with_ifc_prefix_stripped(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    class_dd = page.locator("#property-panel dt", has_text="class").locator(
        "xpath=following-sibling::dd[1]"
    )
    assert class_dd.locator(".property-class-chip").count() == 1
    assert class_dd.inner_text().strip() == "Wall"  # "IfcWall" -> "Wall"


def test_guid_row_has_copy_button_that_writes_to_clipboard(page, served_url, browser):
    context = page.context
    context.grant_permissions(["clipboard-read", "clipboard-write"])

    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    guid_dd = page.locator("#property-panel dt", has_text="GUID").locator("xpath=following-sibling::dd[1]")
    copy_btn = guid_dd.locator(".property-copy-btn")
    assert copy_btn.count() == 1
    copy_btn.click()

    clipboard_text = page.evaluate("() => navigator.clipboard.readText()")
    assert clipboard_text == guid


def test_guid_copy_falls_back_to_text_selection_when_clipboard_unavailable(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    # navigator.clipboardを一時的に無効化し、失敗パスを踏ませる。
    page.evaluate("() => { Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true }); }")

    guid_dd = page.locator("#property-panel dt", has_text="GUID").locator("xpath=following-sibling::dd[1]")
    copy_btn = guid_dd.locator(".property-copy-btn")
    copy_btn.click()

    selected_text = page.evaluate("() => window.getSelection().toString()")
    assert selected_text == guid


def test_latitude_longitude_shown_as_decimal_degrees_with_unit(page, lat_lon_served_url):
    _wait_for_load(page, lat_lon_served_url)
    guid = _guid_by_name(page, "Fixture Site")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    lat_dd = page.locator("#property-panel dt", has_text="Latitude").locator("xpath=following-sibling::dd[1]")
    lon_dd = page.locator("#property-panel dt", has_text="Longitude").locator("xpath=following-sibling::dd[1]")

    # 35度40分44秒 -> 35 + 40/60 + 44/3600 = 35.678888...
    assert lat_dd.inner_text().strip() == "35.678889°"
    # 139度45分30秒 -> 139 + 45/60 + 30/3600 = 139.758333...
    assert lon_dd.inner_text().strip() == "139.758333°"


def test_latitude_longitude_not_fabricated_when_site_has_no_georeference(page, served_url):
    """コードレビュー指摘の回帰テスト: usd.pyのset_custom_dataはIfcSiteである限り
    RefLatitude/RefLongitude未設定でもLatitude/Longitude customData自体は
    (空文字列として)書き込む。"".split(".")は[""]でNumber("")は0(NaNではない)
    のため、空文字列を弾く分岐が無いと実在しない"0.000000°"を表示してしまって
    いた。tests/fixtures/minimal.ifcのSiteは緯度経度未設定なので、この
    フィクスチャで直接再現できる。"""
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Fixture Site")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    lat_dt_count = page.locator("#property-panel dt", has_text="Latitude").count()
    if lat_dt_count == 0:
        return  # customData自体に含まれていなければ何も表示されないので問題ない
    lat_dd = page.locator("#property-panel dt", has_text="Latitude").locator("xpath=following-sibling::dd[1]")
    assert lat_dd.inner_text().strip() != "0.000000°"


def test_guide_text_shown_when_nothing_selected(page, served_url):
    _wait_for_load(page, served_url)
    assert page.locator("#property-panel .property-guide").count() == 1
    assert page.locator("#property-panel .property-guide").inner_text().strip() != ""


def test_live_data_placeholder_exists_when_element_selected(page, served_url):
    """E9(digital-twin-spec.md)連携用のLive Dataセクション挿入位置。"""
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    assert page.locator("#property-panel #property-live-data").count() == 1
