"""What a Mods package holds, what breaks without it and who uses it."""

import os
import struct

import pytest

from test_plan import CC, casp_bytes, geom_bytes, world, write  # noqa: F401  (fixture)

from traytrim import restypes
from traytrim.dbpf import ResourceKey
from traytrim.depindex import DependencyIndex
from traytrim.index import ResourceIndex
from traytrim.pkgreport import TrayUsage, dependents, package_report, users
from traytrim.tray import TrayItem


def test_recolor_breaks_without_its_mesh_package(tmp_path, world):  # noqa: F811
    index, k = world
    mods = tmp_path / "Mods"
    with DependencyIndex(tmp_path / "deps.sqlite") as deps:
        deps.update(index)
        mesh_pack = package_report(mods / "mesh pack.package", index)
        assert mesh_pack.rel == "mesh pack.package" and mesh_pack.loaded == [k["shared_mesh"]]
        broken = dependents(mesh_pack, deps, index)
        assert [d.rel for d in broken] == ["recolor.package"]
        assert [(n.key, n.by) for n in broken[0].needs] == [(k["shared_mesh"], k["recolor"])]
        # Nothing needs the recolor itself; its CAS part is an item of its own.
        recolor = package_report(mods / "recolor.package", index)
        assert dependents(recolor, deps, index) == []
        assert [r.ref.instance for r in recolor.items] == [k["recolor"].instance]


def test_a_copy_loaded_later_shadows_and_nothing_breaks(tmp_path, world):  # noqa: F811
    index, k = world
    mods = tmp_path / "Mods"
    with DependencyIndex(tmp_path / "deps.sqlite") as deps:
        deps.update(index)
        first = package_report(mods / "slider.package", index)
        assert {key for key, _hit in first.shadowed} == {k["slider"], k["shape"]}
        assert first.loaded == [] and dependents(first, deps, index) == []
        last = package_report(mods / "zz slider copy.package", index)
        assert set(last.loaded) == {k["slider"], k["shape"]} and not last.shadowed


def test_users_follow_references_backwards(tmp_path, world):  # noqa: F811
    index, k = world
    mods = tmp_path / "Mods"
    household = TrayItem(7, 1, "Moreau")
    # The household wears the recolor, which uses the mesh in another package.
    usage = TrayUsage({7: household}, {k["recolor"]: {7}})
    with DependencyIndex(tmp_path / "deps.sqlite") as deps:
        deps.update(index)
        assert users(package_report(mods / "mesh pack.package", index), deps, usage, index) == [household]
        assert users(package_report(mods / "recolor.package", index), deps, usage, index) == [household]
        assert users(package_report(mods / "hair.package", index), deps, usage, index) == []


def test_changed_and_removed_packages_are_read_again(tmp_path, world):  # noqa: F811
    index, k = world
    mods, game = tmp_path / "Mods", tmp_path / "Game"
    with DependencyIndex(tmp_path / "deps.sqlite") as deps:
        deps.update(index)
        # The recolor now points at its own mesh, and the mesh pack is deleted.
        own = ResourceKey(restypes.GEOM, CC, 0x301)
        write(mods / "recolor.package", [(k["recolor"], casp_bytes([own])), (own, b"mesh")])
        st = os.stat(mods / "recolor.package")
        os.utime(mods / "recolor.package", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        (mods / "mesh pack.package").unlink()
        index.update(mods, game)
        deps.update(index)
        needs = deps.needing([k["shared_mesh"], own])
        assert [(n.key, n.by) for n in needs] == [(own, k["recolor"])]


def test_objects_find_their_mesh_in_another_package_even_when_it_comes_later(tmp_path):
    mods, model_id = tmp_path / "Mods", 0xAAAA0000000000F1
    chair = ResourceKey(restypes.OBJECT_DEFINITION, 0, 0xAAAA00000000000A)
    model = ResourceKey(restypes.MODEL, 0, model_id)
    write(mods / "chair recolor.package", [(chair, b"objd" + struct.pack("<Q", model_id))])
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        assert deps.needing([model]) == []  # no mesh package yet
        write(mods / "chair mesh.package", [(model, b"modl")])
        index.update(mods, None)
        deps.update(index)
        mesh = package_report(mods / "chair mesh.package", index)
        assert [d.rel for d in dependents(mesh, deps, index)] == ["chair recolor.package"]


def test_a_package_that_cant_be_read_is_read_again_next_time(tmp_path, world, monkeypatch):  # noqa: F811
    from traytrim import dbpf

    index, k = world
    recolor = tmp_path / "Mods" / "recolor.package"
    real_package = dbpf.Package

    def locked(path):  # locked by another program, say (chmod can't do that on Windows)
        if os.fspath(path) == os.fspath(recolor):
            raise PermissionError(13, "The process cannot access the file")
        return real_package(path)

    with DependencyIndex(tmp_path / "deps.sqlite") as deps:
        monkeypatch.setattr(dbpf, "Package", locked)
        deps.update(index)
        assert deps.unreadable == [recolor]
        assert deps.needing([k["shared_mesh"]]) == []
        monkeypatch.setattr(dbpf, "Package", real_package)
        deps.update(index)  # nothing changed on disk, but it wasn't stored as read
        assert deps.unreadable == []
        assert [n.by for n in deps.needing([k["shared_mesh"]])] == [k["recolor"]]


def test_a_texture_referenced_in_another_format_is_still_needed(tmp_path):
    # The recolor names DST:0:X; only an RLE2 with instance X exists. The export plan takes the
    # RLE2 for it, so its package must not look safe to delete.
    mods, x = tmp_path / "Mods", 0xBBBB000000000201
    recolor = ResourceKey(restypes.CASP, CC, 0x200)
    write(mods / "recolor.package",
          [(recolor, casp_bytes([ResourceKey(restypes.DST, 0, x)]))])
    write(mods / "textures.package", [(ResourceKey(restypes.RLE2, 0, x), b"pixels")])
    household = TrayItem(7, 1, "Moreau")
    usage = TrayUsage({7: household}, {recolor: {7}})
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        textures = package_report(mods / "textures.package", index)
        assert [d.rel for d in dependents(textures, deps, index)] == ["recolor.package"]
        assert users(textures, deps, usage, index) == [household]


def test_matching_against_the_assets_finds_what_all_values_find(tmp_path, monkeypatch):
    import traytrim.depindex

    # Matched against all the assets in Mods even for two packages (a big first build).
    monkeypatch.setattr(traytrim.depindex, "_ASSETS_FROM", 0)
    test_objects_find_their_mesh_in_another_package_even_when_it_comes_later(tmp_path)


def test_a_cancel_after_reading_a_new_mesh_doesnt_lose_who_needs_it(tmp_path):
    import threading

    from traytrim.plan import Cancelled

    mods, model_id = tmp_path / "Mods", 0xAAAA0000000000F1
    chair = ResourceKey(restypes.OBJECT_DEFINITION, 0, 0xAAAA00000000000A)
    model = ResourceKey(restypes.MODEL, 0, model_id)
    write(mods / "chair recolor.package", [(chair, b"objd" + struct.pack("<Q", model_id))])
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        write(mods / "chair mesh.package", [(model, b"modl")])  # new; the recolor is unchanged
        write(mods / "zz later.package", [(ResourceKey(restypes.MODEL, 0, 0xAAAA0000000000F2),
                                           b"modl")])  # read after the mesh, so the cancel lands
        index.update(mods, None)
        cancel = threading.Event()
        with pytest.raises(Cancelled):  # cancelled right after reading the mesh package
            deps.update(index, lambda phase, done, total: cancel.set(), cancel)
        deps.update(index)
        mesh = package_report(mods / "chair mesh.package", index)
        assert [d.rel for d in dependents(mesh, deps, index)] == ["chair recolor.package"]


def test_what_uses_a_broken_mesh_breaks_too(tmp_path):
    # textures.package <- mesh.package <- recolor.package: without the textures, the mesh
    # breaks, and with it the recolor, which is listed through the mesh package.
    mods = tmp_path / "Mods"
    tex = ResourceKey(restypes.RLE2, CC, 0xBBBB000000000301)
    mesh = ResourceKey(restypes.GEOM, CC, 0xBBBB000000000302)
    recolor = ResourceKey(restypes.CASP, CC, 0xBBBB000000000303)
    write(mods / "textures.package", [(tex, b"pixels")])
    write(mods / "mesh.package", [(mesh, geom_bytes([tex]))])
    write(mods / "recolor.package", [(recolor, casp_bytes([mesh]))])
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        broken = dependents(package_report(mods / "textures.package", index), deps, index)
        assert [(d.rel, d.through) for d in broken] == [
            ("mesh.package", ""), ("recolor.package", "mesh.package")]


def test_only_the_copy_the_game_loads_counts(tmp_path):
    # Both hold the same CAS part; the game loads the one that sorts last ("b ..."), which
    # reaches the texture through a mesh package. The other copy breaks nothing: it's unused.
    mods = tmp_path / "Mods"
    tex = ResourceKey(restypes.RLE2, CC, 0xBBBB000000000401)
    mesh = ResourceKey(restypes.GEOM, CC, 0xBBBB000000000402)
    part = ResourceKey(restypes.CASP, CC, 0xBBBB000000000403)
    write(mods / "textures.package", [(tex, b"pixels")])
    write(mods / "mesh.package", [(mesh, geom_bytes([tex]))])
    write(mods / "a direct.package", [(part, casp_bytes([tex]))])
    write(mods / "b through mesh.package", [(part, casp_bytes([mesh]))])
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        broken = dependents(package_report(mods / "textures.package", index), deps, index)
        assert [(d.rel, d.through) for d in broken] == [
            ("mesh.package", ""), ("b through mesh.package", "mesh.package")]


def test_a_mesh_replaced_by_a_later_copy_breaks_nothing_through_it(tmp_path):
    # mesh.package's mesh needs the texture, but "zz newer mesh" holds the copy the game loads,
    # which doesn't. The recolor using that mesh isn't broken by deleting the textures.
    mods = tmp_path / "Mods"
    tex = ResourceKey(restypes.RLE2, CC, 0xBBBB000000000601)
    mesh = ResourceKey(restypes.GEOM, CC, 0xBBBB000000000602)
    write(mods / "textures.package", [(tex, b"pixels")])
    write(mods / "mesh.package", [(mesh, geom_bytes([tex]))])
    write(mods / "zz newer mesh.package", [(mesh, geom_bytes([]))])
    write(mods / "recolor.package",
          [(ResourceKey(restypes.CASP, CC, 0xBBBB000000000603), casp_bytes([mesh]))])
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        assert dependents(package_report(mods / "textures.package", index), deps, index) == []


def test_an_objects_take_along_files_count(tmp_path):
    # An object takes everything under its instance from its own package (as the export plan
    # does): here a mesh whose loaded copy lives in another package and needs the textures.
    mods = tmp_path / "Mods"
    inst = 0xBBBB000000000501
    tex = ResourceKey(restypes.RLE2, CC, 0xBBBB000000000502)
    objd = ResourceKey(restypes.OBJECT_DEFINITION, 0, inst)
    sibling = ResourceKey(restypes.GEOM, CC, inst)
    write(mods / "textures.package", [(tex, b"pixels")])
    write(mods / "chair.package", [(objd, b"objd"), (sibling, geom_bytes([]))])
    write(mods / "zz newer mesh.package", [(sibling, geom_bytes([tex]))])  # loads last
    household = TrayItem(7, 2, "Seaside")
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        index.update(mods, None)
        deps.update(index)
        textures = package_report(mods / "textures.package", index)
        assert {d.rel for d in dependents(textures, deps, index)} == {
            "zz newer mesh.package", "chair.package"}
        usage = TrayUsage({7: household}, {objd: {7}})
        assert users(textures, deps, usage, index) == [household]


def test_scan_usage_reads_every_tray_item(tmp_path):
    import test_tray as tt

    from traytrim import tray
    from traytrim.pkgreport import scan_usage

    mods, tray_dir = tmp_path / "Mods", tmp_path / "Tray"
    top = ResourceKey(restypes.CASP, CC, tt.TOP)
    write(mods / "top.package", [(top, casp_bytes([]))])
    tray_dir.mkdir()
    (tray_dir / f"0x00000001!0x{tt.HH_ID:016x}.trayitem").write_bytes(tt.trayitem())
    (tray_dir / f"0x00000000!0x{tt.HH_ID:016x}.householdbinary").write_bytes(
        tt.householdbinary(2))
    (tray_dir / "0x00000001!0x00000000000000aa.trayitem").write_bytes(tt.trayitem())  # no data
    items, _failed = tray.scan_tray(tray_dir)
    progress = []
    with ResourceIndex(tmp_path / "index.sqlite") as index:
        index.update(mods, None)
        usage = scan_usage(items, index, lambda done, total: progress.append((done, total)))
    assert usage.by_key[top] == {tt.HH_ID}
    assert len(usage.unreadable) == 1  # the broken one is named
    assert progress == [(1, 2), (2, 2)]  # and counted
