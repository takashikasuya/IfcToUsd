"""ツールバー・デザイントークン・キーボードショートカットのE2Eテスト（Issue #46 / E8-5）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server
from tests.conftest import chromium_launch_kwargs

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture(scope="module")
def served_url(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("toolbar_shortcuts_e2e")
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


# --- ツールバーグループ化 --------------------------------------------------------


def test_toolbar_has_display_and_analysis_groups_with_titles(page, served_url):
    _wait_for_load(page, served_url)

    # text-transform: uppercaseのCSSにより表示上は大文字になるため、
    # inner_text()は大文字を返す(tests/test_property_panel_e2e.pyの
    # dt要素と同じ理由)。大文字小文字を無視して比較する。
    labels = [label.lower() for label in page.locator(".toolbar-group-label").all_inner_texts()]
    assert "display" in labels
    assert "analysis" in labels

    wireframe_title = page.locator("#wireframe-toggle").locator("xpath=..").get_attribute("title")
    assert wireframe_title and len(wireframe_title) > 0


# --- デザイントークン -------------------------------------------------------------


def test_css_custom_properties_define_bg_and_accent(page, served_url):
    _wait_for_load(page, served_url)

    tokens = page.evaluate("""
        () => {
            const style = getComputedStyle(document.documentElement);
            return {
                bg: style.getPropertyValue('--bg').trim(),
                accent: style.getPropertyValue('--accent').trim(),
                border: style.getPropertyValue('--border').trim(),
            };
        }
    """)
    assert tokens["bg"] == "#202020"
    assert tokens["accent"] == "#3355ff"
    assert tokens["border"]


def test_scene_background_and_highlight_color_resolve_from_css_tokens(page, served_url):
    """viewer.js内のscene.background/選択ハイライト色が、getComputedStyleで
    :rootのCSSカスタムプロパティから解決されていること(ハードコードの二重管理
    を避ける、ux-spec.md §3.5)。"""
    _wait_for_load(page, served_url)

    bg_hex = page.evaluate("window.ifc2usdViewer.scene.background.getHexString()")
    assert bg_hex == "202020"

    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    # 選択中の要素(guid一致)だけを見る。フィルタせず最後に見つかったメッシュを
    # 使うと、非選択の壁(emissiveは既定の000000)を拾ってしまいかねない。
    emissive_hex = page.evaluate(f"""
        () => {{
            let hex = null;
            window.ifc2usdViewer.getGlbRoot().traverse((child) => {{
                if (child.isMesh && child.userData.guid === {guid!r} && child.material.emissive) {{
                    hex = child.material.emissive.getHexString();
                }}
            }});
            return hex;
        }}
    """)
    assert emissive_hex == "3355ff"


# --- パネル開閉 -------------------------------------------------------------------


def test_toggling_tree_panel_collapses_it_and_viewport_grows(page, served_url):
    _wait_for_load(page, served_url)

    viewport_width_before = page.evaluate("document.getElementById('viewport').clientWidth")

    page.locator("#tree-panel-toggle").click()
    assert page.evaluate("document.getElementById('tree-panel').classList.contains('collapsed')") is True

    page.wait_for_timeout(250)  # CSSトランジション(0.15s)を待つ
    viewport_width_after = page.evaluate("document.getElementById('viewport').clientWidth")
    assert viewport_width_after > viewport_width_before

    # 再度押すと復元する
    page.locator("#tree-panel-toggle").click()
    assert page.evaluate("document.getElementById('tree-panel').classList.contains('collapsed')") is False


def test_toggling_property_panel_collapses_it_and_viewport_grows(page, served_url):
    _wait_for_load(page, served_url)

    viewport_width_before = page.evaluate("document.getElementById('viewport').clientWidth")

    page.locator("#property-panel-toggle").click()
    assert page.evaluate("document.getElementById('property-panel').classList.contains('collapsed')") is True

    page.wait_for_timeout(250)
    viewport_width_after = page.evaluate("document.getElementById('viewport').clientWidth")
    assert viewport_width_after > viewport_width_before


# --- キーボードショートカット -----------------------------------------------------


def test_shortcut_f_fits_camera_to_selection(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    # フィット前に、選択要素のbox中心とは無関係な位置へカメラをずらしておく。
    page.evaluate("""
        () => {
            window.ifc2usdViewer.camera.position.set(100, 100, 100);
            window.ifc2usdViewer.controls.target.set(0, 0, 0);
        }
    """)

    page.locator("#viewport").click(position={"x": 5, "y": 5})  # フォーカスをinput要素から外す
    page.keyboard.press("f")

    expected_center = page.evaluate(f"""
        () => {{
            const box = window.ifc2usdViewer.getBoundingBoxOfGuid({guid!r});
            return [(box.min.x + box.max.x) / 2, (box.min.y + box.max.y) / 2, (box.min.z + box.max.z) / 2];
        }}
    """)
    new_target = page.evaluate("window.ifc2usdViewer.controls.target.toArray()")
    for actual, expected in zip(new_target, expected_center):
        assert actual == pytest.approx(expected, abs=1e-3)


def test_shortcut_f_fits_to_whole_model_when_nothing_selected(page, served_url):
    _wait_for_load(page, served_url)

    page.evaluate("""
        () => {
            window.ifc2usdViewer.camera.position.set(100, 100, 100);
            window.ifc2usdViewer.controls.target.set(0, 0, 0);
        }
    """)
    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("f")

    box_center = page.evaluate("""
        () => {
            const box = window.ifc2usdViewer.getBoundingBox();
            return [(box.min.x + box.max.x) / 2, (box.min.y + box.max.y) / 2, (box.min.z + box.max.z) / 2];
        }
    """)
    new_target = page.evaluate("window.ifc2usdViewer.controls.target.toArray()")
    for actual, expected in zip(new_target, box_center):
        assert actual == pytest.approx(expected, abs=1e-3)


def test_shortcut_escape_deselects(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == guid

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("Escape")
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") is None


def test_shortcut_w_toggles_wireframe(page, served_url):
    _wait_for_load(page, served_url)
    assert page.locator("#wireframe-toggle").is_checked() is False

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("w")
    assert page.locator("#wireframe-toggle").is_checked() is True

    page.keyboard.press("w")
    assert page.locator("#wireframe-toggle").is_checked() is False


def test_modifier_plus_shortcut_key_is_left_to_the_browser(page, served_url):
    # Ctrl/Cmd+W(タブを閉じる)・Ctrl/Cmd+F(検索)等のブラウザ標準ショートカットを
    # 奪わないことを確認する(Copilotレビュー指摘、PR #48)。
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")
    page.evaluate("""
        () => {
            window.ifc2usdViewer.camera.position.set(100, 100, 100);
            window.ifc2usdViewer.controls.target.set(0, 0, 0);
        }
    """)
    page.locator("#viewport").click(position={"x": 5, "y": 5})

    page.keyboard.press("Control+w")
    assert page.locator("#wireframe-toggle").is_checked() is False

    page.keyboard.press("Control+f")
    new_target = page.evaluate("window.ifc2usdViewer.controls.target.toArray()")
    assert new_target == pytest.approx([0, 0, 0])


def test_shortcuts_1_2_3_switch_display_mode(page, served_url):
    _wait_for_load(page, served_url)

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("1")
    assert page.evaluate("window.ifc2usdViewer.getDisplayMode()") == "mesh"
    assert page.locator('input[name="display-mode"][value="mesh"]').is_checked()

    page.keyboard.press("2")
    assert page.evaluate("window.ifc2usdViewer.getDisplayMode()") == "voxel"
    assert page.locator('input[name="display-mode"][value="voxel"]').is_checked()

    page.keyboard.press("3")
    assert page.evaluate("window.ifc2usdViewer.getDisplayMode()") == "both"
    assert page.locator('input[name="display-mode"][value="both"]').is_checked()


def test_shortcuts_disabled_while_input_is_focused(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    page.locator("#tree-search-input").click()
    page.keyboard.type("w")  # "w"がタイプされるだけで、ワイヤフレームはトグルされない

    assert page.locator("#wireframe-toggle").is_checked() is False
    assert page.locator("#tree-search-input").input_value() == "w"
    # 選択もEscapeで解除されない(input内でのEscapeを送っても無視される)ことを確認
    page.keyboard.press("Escape")
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == guid


def test_shortcut_question_mark_toggles_overlay(page, served_url):
    _wait_for_load(page, served_url)
    overlay = page.locator("#shortcuts-overlay")
    assert overlay.evaluate("el => el.classList.contains('visible')") is False

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("?")
    assert overlay.evaluate("el => el.classList.contains('visible')") is True

    page.keyboard.press("?")
    assert overlay.evaluate("el => el.classList.contains('visible')") is False


def _boxes_overlap(a, b):
    return a["x"] < b["x"] + b["width"] and a["x"] + a["width"] > b["x"] and a["y"] < b["y"] + b["height"] and a["y"] + a["height"] > b["y"]


def test_shortcuts_overlay_does_not_overlap_the_toolbar(page, served_url):
    """コードレビュー指摘の回帰テスト: #display-controlsはflex-wrapで折り返して
    複数行(縦に伸びる)になり得るため、#shortcuts-overlayを画面隅に固定すると
    ツールバーと重なってしまっていた(既定テストビューポート1000x700で実際に
    再現)。ビューポート中央配置への変更後、両者のバウンディングボックスが
    重ならないことを確認する。"""
    _wait_for_load(page, served_url)

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("?")

    toolbar_box = page.locator("#display-controls").bounding_box()
    overlay_box = page.locator("#shortcuts-overlay").bounding_box()
    assert toolbar_box is not None and overlay_box is not None
    assert not _boxes_overlap(toolbar_box, overlay_box)


def test_escape_closes_shortcuts_overlay_in_preference_to_deselecting(page, served_url):
    _wait_for_load(page, served_url)
    guid = _guid_by_name(page, "Wall North")
    page.evaluate(f"window.ifc2usdViewer.selectByGuid({guid!r})")

    page.locator("#viewport").click(position={"x": 5, "y": 5})
    page.keyboard.press("?")
    assert page.locator("#shortcuts-overlay").evaluate("el => el.classList.contains('visible')") is True

    page.keyboard.press("Escape")
    assert page.locator("#shortcuts-overlay").evaluate("el => el.classList.contains('visible')") is False
    # オーバーレイを閉じるだけで、選択はまだ解除されない
    assert page.evaluate("window.ifc2usdViewer.getSelectedGuid()") == guid
