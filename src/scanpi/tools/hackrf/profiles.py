"""HackRF SDR profile loader / validator / writer.

A profile is a TOML file at ``~/scanpi/profiles/sdrs/<id>.toml`` (or one of
the bundled presets in ``profiles/sdrs/presets/``). The schema mirrors what
RESEARCH_2026-05-04.md section 1b describes: an [sdr] block, a
[channelizer] block, and a list of [[channels]] entries.

Validation rules:
  - every channel must fall inside [center - sr/2, center + sr/2]
  - num_chans must be a positive integer (PFB target M)
  - sample_rate must be a positive int; we warn if >8 Msps on Pi 5 (USB 2.0)
  - each channel gets a computed `output_index` = ((freq - center) / channel_bw) mod M
    so the flowgraph can wire that PFB output port to the demod chain.

The TOML is the source of truth; this module is just a typed view + writer.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

log = logging.getLogger(__name__)


@dataclass
class SdrSection:
    id: str = "hackrf0"
    driver: str = "hackrf"
    serial: str = ""
    center_hz: int = 462_500_000
    sample_rate: int = 8_000_000
    gain: str = "lna=24,vga=20,amp=0"
    front_end: str = ""           # description of physical filter, not a control
    fake_iq_path: str = ""        # if set, source IQ from this file (no hardware)


@dataclass
class ChannelizerSection:
    type: str = "pfb"
    num_chans: int = 32
    attenuation_db: float = 80.0
    taps_window: str = "blackman-harris"
    transition_frac: float = 0.20  # transition BW = 20% of channel BW (Rondeau rule of thumb)


@dataclass
class ChannelSpec:
    name: str
    freq_hz: int
    demod: str = "nfm"            # 'nfm', 'wfm', 'am'
    bw_hz: int = 12_500
    squelch_db: float = -25.0
    deemph_us: float = 75.0       # de-emphasis time constant
    notes: str = ""

    # Filled in by validate(); the channelizer output port that contains this
    # channel's center frequency. PFB output ordering is the FFT bin layout.
    output_index: int = -1
    # Net frequency offset of the channel center inside its PFB output bin
    # (Hz). The downstream demod can apply a small freq-xlating filter to pull
    # this exactly to DC. Usually zero if the channel grid aligns with the bins.
    bin_offset_hz: float = 0.0


@dataclass
class Profile:
    sdr: SdrSection = field(default_factory=SdrSection)
    channelizer: ChannelizerSection = field(default_factory=ChannelizerSection)
    channels: list[ChannelSpec] = field(default_factory=list)
    # The path the profile was loaded from, if any (for round-trip writes).
    source_path: Path | None = None

    @property
    def channel_bw_hz(self) -> int:
        """Per-PFB-output bandwidth = sample_rate / num_chans."""
        return self.sdr.sample_rate // self.channelizer.num_chans

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sdr": {k: v for k, v in asdict(self.sdr).items() if v != "" or k in ("serial",)},
            "channelizer": asdict(self.channelizer),
            "channels": [
                {k: v for k, v in asdict(ch).items()
                 if k not in ("output_index", "bin_offset_hz")}
                for ch in self.channels
            ],
        }
        return out


# --------------------------------------------------------------- defaults dir


def user_profiles_dir() -> Path:
    """Where user-editable SDR profiles live (~/scanpi/profiles/sdrs/)."""
    return Path.home() / "scanpi" / "profiles" / "sdrs"


def presets_dir() -> Path:
    """Bundled preset directory inside the repo (profiles/sdrs/presets/)."""
    here = Path(__file__).resolve()
    # tools/hackrf/profiles.py -> repo root is parent[4]
    repo_root = here.parents[4]
    return repo_root / "profiles" / "sdrs" / "presets"


def list_user_profiles() -> list[Path]:
    d = user_profiles_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.toml"))


def list_presets() -> list[Path]:
    d = presets_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.toml"))


# ----------------------------------------------------------- load / validate


def _coerce_int(v: Any, label: str) -> int:
    if isinstance(v, bool):  # bool is subclass of int — exclude
        raise ValueError(f"{label} must be int, got bool")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        # allow "8_000_000" or "8000000" (TOML already supports underscores natively
        # in numeric literals; this branch is just for stringly-typed values).
        return int(v.replace("_", ""))
    raise ValueError(f"{label} must be int, got {type(v).__name__}")


def parse_profile(data: dict[str, Any], source_path: Path | None = None) -> Profile:
    """Build a Profile from a parsed TOML dict. Does NOT validate."""
    s = data.get("sdr", {})
    sdr = SdrSection(
        id=str(s.get("id", "hackrf0")),
        driver=str(s.get("driver", "hackrf")),
        serial=str(s.get("serial", "")),
        center_hz=_coerce_int(s.get("center_hz", 462_500_000), "sdr.center_hz"),
        sample_rate=_coerce_int(s.get("sample_rate", 8_000_000), "sdr.sample_rate"),
        gain=str(s.get("gain", "lna=24,vga=20,amp=0")),
        front_end=str(s.get("front_end", "")),
        fake_iq_path=str(s.get("fake_iq_path", "")),
    )
    c = data.get("channelizer", {})
    channelizer = ChannelizerSection(
        type=str(c.get("type", "pfb")),
        num_chans=_coerce_int(c.get("num_chans", 32), "channelizer.num_chans"),
        attenuation_db=float(c.get("attenuation_db", 80.0)),
        taps_window=str(c.get("taps_window", "blackman-harris")),
        transition_frac=float(c.get("transition_frac", 0.20)),
    )
    channels = []
    for raw in data.get("channels", []) or []:
        channels.append(ChannelSpec(
            name=str(raw["name"]),
            freq_hz=_coerce_int(raw["freq_hz"], f"channels[{raw.get('name','?')}].freq_hz"),
            demod=str(raw.get("demod", "nfm")).lower(),
            bw_hz=_coerce_int(raw.get("bw_hz", 12_500), f"channels[{raw.get('name','?')}].bw_hz"),
            squelch_db=float(raw.get("squelch_db", -25.0)),
            deemph_us=float(raw.get("deemph_us", 75.0)),
            notes=str(raw.get("notes", "")),
        ))
    return Profile(sdr=sdr, channelizer=channelizer, channels=channels, source_path=source_path)


def load_profile(path: str | Path) -> Profile:
    """Load+validate a TOML profile from disk."""
    p = Path(path).expanduser()
    with open(p, "rb") as fh:
        data = tomllib.load(fh)
    prof = parse_profile(data, source_path=p)
    validate_profile(prof)
    return prof


def parse_text(text: str, source_path: Path | None = None) -> Profile:
    """Parse a TOML string (e.g. from an upload) into a validated Profile."""
    data = tomllib.loads(text)
    prof = parse_profile(data, source_path=source_path)
    validate_profile(prof)
    return prof


def validate_profile(prof: Profile) -> None:
    """In-place validation: throws ValueError on first problem; fills output_index.

    Rules:
      - sample_rate > 0
      - num_chans > 0
      - each channel falls inside [center - sr/2, center + sr/2]
      - demod in known set
    Computes:
      - channel.output_index (PFB output port)
      - channel.bin_offset_hz (residual offset inside the bin)
    """
    sr = prof.sdr.sample_rate
    if sr <= 0:
        raise ValueError("sdr.sample_rate must be > 0")
    M = prof.channelizer.num_chans
    if M <= 0:
        raise ValueError("channelizer.num_chans must be > 0")
    chan_bw = sr // M
    if chan_bw <= 0:
        raise ValueError("computed channel bandwidth is zero — sample_rate too small or num_chans too large")

    half = sr / 2.0
    lo = prof.sdr.center_hz - half
    hi = prof.sdr.center_hz + half

    known_demods = {"nfm", "wfm", "am"}
    seen_names: set[str] = set()

    for ch in prof.channels:
        if ch.name in seen_names:
            raise ValueError(f"duplicate channel name: {ch.name!r}")
        seen_names.add(ch.name)
        if ch.demod not in known_demods:
            raise ValueError(f"channel {ch.name!r}: unknown demod {ch.demod!r} "
                             f"(known: {sorted(known_demods)})")
        if not (lo <= ch.freq_hz <= hi):
            raise ValueError(
                f"channel {ch.name!r} freq {ch.freq_hz} Hz is outside the "
                f"capture window [{int(lo)}, {int(hi)}] Hz "
                f"(center={prof.sdr.center_hz}, sr={sr})"
            )
        # PFB output port indexing.  Per the GR doxygen page, output 0 is DC,
        # output M-1 is the highest positive bin; with critical sampling
        # (oversample_rate=1) the bin layout maps freqs in
        # [center - sr/2 + k*chan_bw, center - sr/2 + (k+1)*chan_bw] to a
        # specific output. The doxygen example wires 'output i' to the
        # complex frequency offset center + (i - M/2) * chan_bw (after the
        # GR convention applies a fft-shift to put DC in the middle).
        rel = ch.freq_hz - prof.sdr.center_hz       # signed offset from center (Hz)
        # Map [-sr/2, sr/2) to bin index [0, M) where bin M/2 is DC.
        bin_idx = int(round(rel / chan_bw)) + (M // 2)
        bin_idx = bin_idx % M
        ch.output_index = bin_idx
        # Residual: where exactly inside the bin the channel center sits.
        center_of_bin_hz = (bin_idx - (M // 2)) * chan_bw
        ch.bin_offset_hz = float(rel - center_of_bin_hz)

    log.debug("profile %s validated: %d channels, %d Hz/channel, sr=%d",
              prof.sdr.id, len(prof.channels), chan_bw, sr)


# ---------------------------------------------------------------- write back


def _format_toml(prof: Profile) -> str:
    """Hand-rolled TOML serializer — Python stdlib has tomllib (read-only).
    Keeps comments stable for round-tripping a profile through the API.
    """
    s = prof.sdr
    c = prof.channelizer
    out: list[str] = []
    out.append("# ScanPi SDR profile — HackRF One")
    out.append("# Sections: [sdr], [channelizer], one [[channels]] per channel.")
    out.append("# Validation: every channel.freq_hz must fall inside")
    out.append("#   [sdr.center_hz - sdr.sample_rate/2, sdr.center_hz + sdr.sample_rate/2].")
    out.append("")
    out.append("[sdr]")
    out.append(f'id          = "{s.id}"')
    out.append(f'driver      = "{s.driver}"')
    out.append(f'serial      = "{s.serial}"')
    out.append(f"center_hz   = {s.center_hz}")
    out.append(f"sample_rate = {s.sample_rate}")
    out.append(f'gain        = "{s.gain}"')
    if s.front_end:
        out.append(f'front_end   = "{s.front_end}"')
    if s.fake_iq_path:
        out.append(f'fake_iq_path = "{s.fake_iq_path}"')
    out.append("")
    out.append("[channelizer]")
    out.append(f'type           = "{c.type}"')
    out.append(f"num_chans      = {c.num_chans}")
    out.append(f"attenuation_db = {c.attenuation_db}")
    out.append(f'taps_window    = "{c.taps_window}"')
    out.append(f"transition_frac = {c.transition_frac}")
    out.append("")
    for ch in prof.channels:
        out.append("[[channels]]")
        out.append(f'name       = "{ch.name}"')
        out.append(f"freq_hz    = {ch.freq_hz}")
        out.append(f'demod      = "{ch.demod}"')
        out.append(f"bw_hz      = {ch.bw_hz}")
        out.append(f"squelch_db = {ch.squelch_db}")
        out.append(f"deemph_us  = {ch.deemph_us}")
        if ch.notes:
            # Escape any quotes
            escaped = ch.notes.replace('"', '\\"')
            out.append(f'notes      = "{escaped}"')
        out.append("")
    return "\n".join(out)


def save_profile(prof: Profile, path: str | Path | None = None) -> Path:
    """Persist a profile back to disk. Returns the destination path."""
    if path is None:
        if prof.source_path is None:
            d = user_profiles_dir()
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{prof.sdr.id}.toml"
        else:
            path = prof.source_path
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = _format_toml(prof)
    p.write_text(text, encoding="utf-8")
    prof.source_path = p
    return p


def install_preset(name: str, dest_id: str | None = None) -> Path:
    """Copy a bundled preset (by basename) into the user profiles dir.

    e.g. install_preset("hackrf_gmrs_frs") copies presets/hackrf_gmrs_frs.toml
    -> ~/scanpi/profiles/sdrs/hackrf_gmrs_frs.toml.
    """
    src = presets_dir() / f"{name}.toml"
    if not src.exists():
        # Allow caller to pass with or without .toml suffix
        candidate = presets_dir() / name
        if candidate.exists() and candidate.suffix == ".toml":
            src = candidate
        else:
            raise FileNotFoundError(f"preset not found: {src}")
    dest_dir = user_profiles_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (f"{dest_id}.toml" if dest_id else src.name)
    shutil.copy2(src, dest)
    log.info("installed preset %s -> %s", name, dest)
    return dest


def find_default_profile() -> Path | None:
    """Return the first user profile (alphabetical), or first preset, or None."""
    user = list_user_profiles()
    if user:
        return user[0]
    pres = list_presets()
    if pres:
        return pres[0]
    return None
