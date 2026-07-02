"""Avatar ingest and pre-render pipeline for teammate-comms.

Pillow is imported lazily, ONLY inside ``ingest_avatar``.  The module top-level
is pure stdlib — this module never breaks the zero-dep hot path.
"""

import base64
import hashlib
import io
from pathlib import Path

from .comms import (
    CommsError,
    file_lock_optional,
    get_agents_dir,
    get_avatars_dir,
    now_timestamp,
    read_json_readonly,
    write_agent_record,
    write_bytes_atomic,
    write_json_atomic,
)

_CANVAS = 256         # canonical canvas side (px)
_STRIP_W = 8          # half-block strip width in terminal cells
_STRIP_H = 8          # strip height in half-block rows (each row = 2 source pixels)
_MAX_SRC_BYTES = 50 * 1024 * 1024   # 50 MB byte cap (pre-decode guard)

# xterm-256 color cube level values (indices 16-231)
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)

# Luminance ramp for ASCII (dark → bright)
_LUMA_CHARS = " .:-=+*#%@"


def _nearest_cube_idx(v):
    best_i, best_d = 0, abs(v - _CUBE_LEVELS[0])
    for i in range(1, 6):
        d = abs(v - _CUBE_LEVELS[i])
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def _rgb_to_xterm256(r, g, b):
    """Nearest xterm-256 color index, chosen from cube (16-231) or grayscale ramp (232-255)."""
    ri = _nearest_cube_idx(r)
    gi = _nearest_cube_idx(g)
    bi = _nearest_cube_idx(b)
    cube_idx = 16 + 36 * ri + 6 * gi + bi
    cube_err = (r - _CUBE_LEVELS[ri]) ** 2 + (g - _CUBE_LEVELS[gi]) ** 2 + (b - _CUBE_LEVELS[bi]) ** 2

    lum = round(0.299 * r + 0.587 * g + 0.114 * b)
    gray_step = max(0, min(23, round((lum - 8) / 10)))
    gray_val = 8 + gray_step * 10
    gray_idx = 232 + gray_step
    gray_err = (r - gray_val) ** 2 + (g - gray_val) ** 2 + (b - gray_val) ** 2

    return gray_idx if gray_err < cube_err else cube_idx


def _render_ansi(img):
    """Downscale ``img`` (256×256) to 8×16, then encode as xterm-256 half-block ANSI strip."""
    small = img.resize((_STRIP_W, _STRIP_H * 2), resample=1)  # 1 = LANCZOS
    pixels = small.load()
    lines = []
    for row in range(_STRIP_H):
        segs = []
        for col in range(_STRIP_W):
            tr, tg, tb = pixels[col, row * 2][:3]
            br, bg_v, bb = pixels[col, row * 2 + 1][:3]
            fg = _rgb_to_xterm256(tr, tg, tb)
            bg = _rgb_to_xterm256(br, bg_v, bb)
            segs.append(f"\x1b[38;5;{fg}m\x1b[48;5;{bg}m▀")
        segs.append("\x1b[0m")
        lines.append("".join(segs))
    return "\n".join(lines)


def _render_ascii(img):
    """Downscale ``img`` (256×256) to 8×8, then encode as monochrome ASCII strip."""
    small = img.resize((_STRIP_W, _STRIP_H), resample=1)  # 1 = LANCZOS
    pixels = small.load()
    lines = []
    for row in range(_STRIP_H):
        chars = []
        for col in range(_STRIP_W):
            r, g, b = pixels[col, row][:3]
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
            idx = min(int(lum * len(_LUMA_CHARS)), len(_LUMA_CHARS) - 1)
            chars.append(_LUMA_CHARS[idx])
        lines.append("".join(chars))
    return "\n".join(lines)


def _clear_avatar_record(root, team, name):
    """Pop the 'avatar' key from the agent record under a lock. Returns True on success."""
    agents_dir = get_agents_dir(root, team)
    record_path = agents_dir / f"{name}.json"
    with file_lock_optional(record_path, timeout=5) as acquired:
        if not acquired:
            return False
        record = read_json_readonly(record_path)
        if not isinstance(record, dict):
            return True   # no record → nothing to clear, not an error
        record.pop("avatar", None)
        write_json_atomic(record_path, record)
        return True


def read_avatar_strip(root, team, name, fmt="ansi"):
    """Read a cached avatar sidecar for ``name`` (no Pillow, no network).

    Returns the text content, or None if no avatar or file absent.
    ``fmt`` is 'ansi' (xterm-256 half-block) or 'txt' (monochrome ASCII).
    """
    ext = "ansi" if fmt == "ansi" else "txt"
    path = get_avatars_dir(root, team) / f"{name}.{ext}"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def ingest_avatar(root, team, name, *, path=None, image_base64=None, clear=False):
    """Ingest, resize, pre-render, and store an avatar for agent ``name``.

    ``path``         — filesystem path to a source image file.
    ``image_base64`` — base64-encoded image bytes (alternative to path).
    ``clear``        — if True, remove the avatar (record-first, then sidecars).

    Returns the ASCII strip text on success, or None on clear.
    Raises CommsError on any failure (including missing Pillow).
    """
    avatars_dir = get_avatars_dir(root, team)

    if clear:
        # Record-first: drop the avatar key before removing sidecars.
        if not _clear_avatar_record(root, team, name):
            raise CommsError(
                "Could not update agent record to clear avatar (registry busy). Try again."
            )
        for ext in ("png", "ansi", "txt"):
            try:
                (avatars_dir / f"{name}.{ext}").unlink()
            except OSError:
                pass
        return None

    # Lazy Pillow import — the ONLY place PIL enters the process.
    try:
        from PIL import Image
    except ImportError:
        raise CommsError(
            "Pillow is not installed. Set TEAMMATE_AVATARS_ENABLED=1 before launching Claude "
            "Code (re-syncs the plugin venv with the images extra on next session start), or "
            "run: uv sync --project <plugin-root> --extra images"
        )

    # Obtain raw bytes from path or base64.
    if path is not None:
        try:
            src_bytes = Path(path).read_bytes()
        except OSError as exc:
            raise CommsError(f"Could not read image file {path!r}: {exc}")
    elif image_base64 is not None:
        # D3: byte-cap the ENCODED string BEFORE decoding — base64 expands ~4/3x, so decoding
        # first would let an over-cap payload burn CPU/memory on the decode itself before the
        # post-decode check below ever catches it. (4/3 + a 4-byte pad allowance, matching the
        # base64 encoded-length formula: ceil(n/3)*4.)
        if len(image_base64) > _MAX_SRC_BYTES * 4 // 3 + 4:
            raise CommsError(
                "Source image (base64-encoded) exceeds the 50 MB byte cap. "
                "Resize or compress the image first."
            )
        try:
            src_bytes = base64.b64decode(image_base64)
        except Exception as exc:
            raise CommsError(f"Invalid base64 image data: {exc}")
    else:
        raise CommsError("Provide 'path' (image file path) or 'image_base64'.")

    # 50 MB byte cap before touching PIL (CPU-bomb / decompression-bomb guard).
    if len(src_bytes) > _MAX_SRC_BYTES:
        raise CommsError(
            f"Source image exceeds the 50 MB byte cap "
            f"({len(src_bytes) // (1024 * 1024)} MB). "
            "Resize or compress the image first."
        )

    # Pixel-count guard: PIL raises DecompressionBombError at decode time.
    Image.MAX_IMAGE_PIXELS = 50_000_000

    try:
        img = Image.open(io.BytesIO(src_bytes))
        img.load()          # force full decode → triggers MAX_IMAGE_PIXELS check
        img = img.convert("RGB")
    except Exception as exc:
        raise CommsError(f"Could not decode image: {exc}")

    w, h = img.size
    if w == 0 or h == 0:
        raise CommsError("Image has zero dimensions.")

    # Fit to 256×256 canvas preserving aspect ratio; pad remainder with black.
    scale = _CANVAS / max(w, h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (_CANVAS, _CANVAS), (0, 0, 0))
    canvas.paste(resized, ((_CANVAS - new_w) // 2, (_CANVAS - new_h) // 2))

    # Pre-render sidecars in memory.
    ansi_text = _render_ansi(canvas)
    ascii_text = _render_ascii(canvas)
    png_buf = io.BytesIO()
    canvas.save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    # 12-char SHA-256 prefix as the stable avatar hash.
    avatar_hash = hashlib.sha256(png_bytes).hexdigest()[:12]
    updated_at = now_timestamp()

    # Write sidecars atomically (PNG binary, ANSI+ASCII as UTF-8 bytes).
    avatars_dir.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(avatars_dir / f"{name}.png", png_bytes)
    write_bytes_atomic(avatars_dir / f"{name}.ansi", ansi_text.encode("utf-8"))
    write_bytes_atomic(avatars_dir / f"{name}.txt", ascii_text.encode("utf-8"))

    # Update agent record (sidecars already on disk — if this fails the user gets an error
    # and can retry; the sidecars will be silently overwritten on next ingest).
    ok = write_agent_record(
        root, team, name, timeout=5,
        avatar={"hash": avatar_hash, "updated_at": updated_at},
    )
    if not ok:
        raise CommsError(
            "Avatar files written but agent record update failed (registry busy). "
            "Try again — the sidecars are already in place."
        )

    return ascii_text
