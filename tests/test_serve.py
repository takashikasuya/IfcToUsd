"""`ifc2usd serve`（静的配信）のテスト。

ディレクトリ組み立て（scene.json/GLB/静的ビューワーアセットの用意）と、
HTTPサーバーの起動・応答を分離してテストする。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ifc2usd import convert
from ifc2usd.serve import build_serve_directory, make_server

FIXTURE = Path(__file__).parent / "fixtures" / "minimal.ifc"


@pytest.fixture
def usda(tmp_path) -> Path:
    out = tmp_path / "minimal.usda"
    convert(FIXTURE, out)
    return out


def test_build_serve_directory_produces_scene_json_and_glb(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    scene_path = workdir / "scene.json"
    assert scene_path.is_file()
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["version"] == 1
    assert scene["tree"][0]["path"] == "/IFC_Model/Site"

    glb_name = scene["assets"]["gltf"]
    assert (workdir / glb_name).is_file()


def test_build_serve_directory_copies_viewer_assets(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    assert (workdir / "index.html").is_file()
    assert (workdir / "viewer.js").is_file()
    assert (workdir / "vendor" / "three.module.min.js").is_file()
    assert (workdir / "vendor" / "controls" / "OrbitControls.js").is_file()
    assert (workdir / "vendor" / "loaders" / "GLTFLoader.js").is_file()


def test_build_serve_directory_rejects_missing_usd(tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    with pytest.raises(Exception):
        build_serve_directory(tmp_path / "does_not_exist.usda", workdir)


def test_server_serves_scene_json_and_index_html(usda, tmp_path):
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/scene.json", timeout=5) as resp:
            assert resp.status == 200
            scene = json.loads(resp.read().decode("utf-8"))
            assert scene["version"] == 1

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html", timeout=5) as resp:
            assert resp.status == 200
            assert b"<html" in resp.read().lower()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/vendor/three.module.min.js", timeout=5) as resp:
            assert resp.status == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_server_binds_to_localhost_only(usda, tmp_path):
    """外部からの意図しないアクセスを避けるため、127.0.0.1にのみバインドする。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_directory_listing_is_disabled_for_index_less_directories(usda, tmp_path):
    """vendor/のようにindex.htmlを持たないディレクトリでも、
    ファイル名一覧を返さない（404になる）。"""
    workdir = tmp_path / "www"
    workdir.mkdir()
    build_serve_directory(usda, workdir)

    server = make_server(workdir, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/vendor/", timeout=5)
        assert excinfo.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)
