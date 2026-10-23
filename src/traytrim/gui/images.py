"""Loading images out of packages: Sim portraits from the game's thumbnail cache, CAS part and
object thumbnails from the CC packages. Runs on worker threads; returns QImage (thread-safe), never
QPixmap."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage, QImageReader

from traytrim import dbpf, tray
from traytrim.index import ResourceIndex

# Sim portraits in the game's localthumbcache.package: type observed on real data (the cached
# copy of each Sim's .sgi image, keyed by Sim id); the .sgi files themselves are encoded.
SIM_PORTRAIT = 0x56278554


# Larger than any thumbnail or tray picture (those are a few hundred pixels across). The images
# come from CC packages: one whose header claims tens of thousands of pixels each way would be
# decoded at full size before scaling.
MAX_PIXELS = 4096 * 4096


def _decode(data: bytes, size: int) -> QImage | None:
    # The buffer must outlive the reader: a temporary one would be freed while it's read.
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    stated = reader.size()  # from the header, before anything is decoded
    if not stated.isValid() or stated.width() * stated.height() > MAX_PIXELS:
        return None
    image = reader.read()
    if image.isNull():
        return None
    return image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)


def load_tray_pictures(items: list[tray.TrayItem], width: int) -> dict[int, QImage]:
    """Each tray item's own picture (household, lot or room), by item instance, scaled to
    `width` pixels wide."""
    out: dict[int, QImage] = {}
    for item in items:
        path = tray.picture_file(item)
        if path is None:
            continue
        try:
            image = QImage.fromData(tray.read_tray_picture(path))
        except tray.TrayError:
            continue
        if not image.isNull():
            out[item.instance] = image.scaledToWidth(
                width, Qt.TransformationMode.SmoothTransformation)
    return out


def load_portraits(user_dir: Path, sim_ids: set[int], size: int) -> dict[int, QImage]:
    """Portraits the game has cached for these Sims (Sims never shown in-game have none)."""
    return _from_cache(user_dir / "localthumbcache.package", SIM_PORTRAIT, sim_ids, size)


def load_thumbnails(index: ResourceIndex, instances: list[int], thumb_type: int, size: int,
                    user_dir: Path | None = None) -> dict[int, QImage]:
    """A thumbnail for each CC item: the one of `thumb_type` (THUM for CAS parts, OBJECT_THUMB
    for objects and build items) with the same instance in its package when there is one,
    otherwise the one the game generated in localthumbcache.package (many CC packages only
    ship a thumbnail for their first swatch)."""
    out: dict[int, QImage] = {}
    open_packages: dict[Path, dbpf.Package] = {}
    try:
        for instance in instances:
            hits = [h for key in index.keys_with_instance(instance, thumb_type)
                    for h in index.mods_hits(key)]
            if not hits:
                continue
            hit = max(hits, key=lambda h: (h.entry.mem_size, h.load_order))
            try:
                pkg = open_packages.get(hit.path)
                if pkg is None:
                    pkg = open_packages[hit.path] = dbpf.Package(hit.path)
                image = _decode(pkg.read(hit.entry), size)
            except (OSError, dbpf.DbpfError):
                continue
            if image is not None:
                out[instance] = image
    finally:
        for pkg in open_packages.values():
            pkg.close()
    missing = set(instances) - out.keys()
    if missing and user_dir is not None:
        out.update(_from_cache(user_dir / "localthumbcache.package", thumb_type, missing,
                               size))
    return out


def _from_cache(cache: Path, type_id: int, instances: set[int], size: int) -> dict[int, QImage]:
    out: dict[int, QImage] = {}
    try:
        with dbpf.Package(cache) as pkg:
            best: dict[int, dbpf.IndexEntry] = {}
            for e in pkg:
                if (e.key.type == type_id and e.key.instance in instances
                        and (e.key.instance not in best or e.mem_size > best[e.key.instance].mem_size)):
                    best[e.key.instance] = e
            for instance, entry in best.items():
                try:
                    image = _decode(pkg.read(entry), size)
                except dbpf.DbpfError:
                    continue
                if image is not None:
                    out[instance] = image
    except (OSError, dbpf.DbpfError):
        pass
    return out
