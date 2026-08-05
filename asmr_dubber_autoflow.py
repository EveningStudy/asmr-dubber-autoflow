from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Sequence

from autoflow.catalog import (
    Edition,
    ScanResult,
    TrackCandidate,
    natural_key,
    scan_work,
)


TOOL_ROOT = Path(__file__).resolve().parent
WORK_ROOT = TOOL_ROOT / ".work"
STATE_ROOT = TOOL_ROOT / ".state"
SETTINGS_FILE = TOOL_ROOT / "settings.txt"
LOG_FILE = STATE_ROOT / "autoflow.log"
LOG_MAX_BYTES = 8 * 1024 * 1024

DEFAULT_HARMONIZED_VOLUME_REDUCTION_DB = 10.0
DEFAULT_HARMONIZED_DELAY_MINUTES = 20.0
SAMPLE_RATE = 48_000
VIDEO_SIZE = "1920x1080"
VIDEO_FILTER_SIZE = "1920:1080"
VIDEO_FPS = 5
KEYFRAME_INTERVAL_SECONDS = 10
REFERENCE_SELECTION_TIMEOUT_SECONDS = 5 * 60
TIMESTAMP_SCHEMA = 2
PERIODIC_KEYFRAME_OPTIONS = (
    "-g",
    str(VIDEO_FPS * KEYFRAME_INTERVAL_SECONDS),
    "-force_key_frames",
    f"expr:gte(t,n_forced*{KEYFRAME_INTERVAL_SECONDS})",
)

AUDIO_EXTENSIONS = {
    ".wav",
    ".flac",
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".mka",
    ".m4b",
    ".ape",
}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
NUMBERED_NAME = re.compile(r"^\s*(\d+)(?:[\s._\-、]+)?(.*)$")

LAYOUT_MERGED = "merged"
LAYOUT_SEPARATE = "separate"
LAYOUT_BOTH = "both"
LAYOUT_ALIASES = {
    "merged": LAYOUT_MERGED,
    "merge": LAYOUT_MERGED,
    "separate": LAYOUT_SEPARATE,
    "split": LAYOUT_SEPARATE,
    "both": LAYOUT_BOTH,
}

MODE_AUDIO = "audio"
MODE_VIDEO_NORMAL = "video_normal"
MODE_VIDEO_HARMONIZED = "video_harmonized"
MODE_ALIASES = {
    "audio": MODE_AUDIO,
    "video-normal": MODE_VIDEO_NORMAL,
    "video-harmonized": MODE_VIDEO_HARMONIZED,
    MODE_VIDEO_NORMAL: MODE_VIDEO_NORMAL,
    MODE_VIDEO_HARMONIZED: MODE_VIDEO_HARMONIZED,
    # Compatibility with tasks and commands created by the personal version.
    "normal": MODE_VIDEO_NORMAL,
    "harmonized": MODE_VIDEO_HARMONIZED,
}

STATUS_ORDER = {
    "media_ready": 10,
    "project_created": 20,
    "analyzed": 30,
    "awaiting_reference": 40,
    "synthesized": 50,
    "mixed": 60,
    "subtitles_ready": 70,
    "outputs_ready": 80,
    "completed": 90,
}

TITLE_TRANSLATION_PROMPT = """你负责把日语作品文件夹名称和音频曲目标题翻译成自然、简洁的简体中文。
保留人物名、编号、括号、符号和作品专有名词，不要省略成人内容，不要解释。
每个输入 id 必须输出一项，顺序和 id 必须完全一致，译文不得为空。
只输出严格 JSON：
{"translations":[{"id":"title0001","zh":"中文标题"}]}"""

DEFAULT_TIMESTAMP_FOOTER = """双语音声制作器：BV1f43G6YEov
内嵌字幕和配音为本地AI生成。内容仅供参考。
仅供日语学习，有能力请购买正版支持。"""


class VideoPreparerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    asmr_root: Path | None
    harmonized_volume_db: float
    harmonized_delay_seconds: int
    timestamp_footer: str
    output_folder_name: str = "AutoFlow输出"
    default_output_layout: str = "ask"
    preferred_audio_formats: tuple[str, ...] = (".wav", ".flac", ".ape", ".m4a", ".mp3")
    bonus_policy: str = "ask"
    background_policy: str = "auto"


@dataclass(frozen=True)
class ToolPaths:
    asmr_root: Path
    asmr_home: Path
    python: Path
    cli_script: Path
    launcher: Path
    ffmpeg: Path
    ffprobe: Path
    powershell: str
    video_encoder_options: tuple[str, ...]


@dataclass(frozen=True)
class AudioSource:
    order: int
    path: Path
    title_ja: str
    size: int
    mtime_ns: int
    relative_path: str = ""
    category: str = "main"
    transcript_path: Path | None = None
    transcript_language: str | None = None
    transcript_timed: bool = False
    source_language: str = "ja"


def print_header() -> None:
    print()
    print("=" * 68)
    print("  ASMR-Dubber AutoFlow · 音频 / 静态视频 / 双语制作")
    print("=" * 68)
    print(f"日志：{LOG_FILE}")


def append_log(text: str) -> None:
    """Append console output to a UTF-8 log without exposing API keys."""

    if not text:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.is_file() and LOG_FILE.stat().st_size >= LOG_MAX_BYTES:
            rotated = LOG_FILE.with_suffix(".log.1")
            rotated.unlink(missing_ok=True)
            os.replace(LOG_FILE, rotated)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except OSError:
        # Logging must never stop a media task.
        pass


def log_event(message: str) -> None:
    append_log(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}\n")


def normalize_mode(value: Any) -> str:
    normalized = MODE_ALIASES.get(str(value or "").strip().casefold())
    if normalized is None:
        raise VideoPreparerError(f"未知模式：{value}")
    return normalized


def normalize_layout(value: Any) -> str:
    normalized = LAYOUT_ALIASES.get(str(value or "").strip().casefold())
    if normalized is None:
        raise VideoPreparerError(f"未知成品组织方式：{value}")
    return normalized


def mode_label(mode: str) -> str:
    return {
        MODE_AUDIO: "纯音频模式",
        MODE_VIDEO_NORMAL: "视频模式 · 普通",
        MODE_VIDEO_HARMONIZED: "视频模式 · 和谐",
    }[normalize_mode(mode)]


def layout_label(layout: str) -> str:
    return {
        LAYOUT_MERGED: "合并成一部",
        LAYOUT_SEPARATE: "每条音轨分别输出",
        LAYOUT_BOTH: "分轨输出 + 合并版",
    }[normalize_layout(layout)]


def load_app_config(path: Path = SETTINGS_FILE) -> AppConfig:
    values: dict[str, str] = {}
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise VideoPreparerError(f"无法读取设置文件：{path}: {exc}") from exc
        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if "=" not in line:
                raise VideoPreparerError(
                    f"设置文件第 {line_number} 行缺少等号：{raw_line}"
                )
            key, value = line.split("=", 1)
            key = key.strip().casefold()
            if key not in {
                "asmr_dubber_path",
                "harmonized_volume_reduction_db",
                "harmonized_delay_minutes",
                "output_folder_name",
                "default_output_layout",
                "preferred_audio_formats",
                "bonus_policy",
                "background_policy",
            } and not re.fullmatch(r"timestamp_footer_line_[1-5]", key):
                print(f"警告：忽略 settings.txt 中的未知设置：{key}")
                continue
            values[key] = value.strip().strip('"').strip("'")

    configured_root = values.get("asmr_dubber_path", "").strip()
    asmr_root: Path | None = None
    if configured_root:
        candidate = Path(configured_root).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        asmr_root = candidate.resolve()

    try:
        reduction = float(
            values.get(
                "harmonized_volume_reduction_db",
                str(DEFAULT_HARMONIZED_VOLUME_REDUCTION_DB),
            )
        )
        delay_minutes = float(
            values.get(
                "harmonized_delay_minutes",
                str(DEFAULT_HARMONIZED_DELAY_MINUTES),
            )
        )
    except ValueError as exc:
        raise VideoPreparerError("settings.txt 中的音量或延后时间不是有效数字。") from exc
    if not 0 <= reduction <= 60:
        raise VideoPreparerError("harmonized_volume_reduction_db 必须在 0 到 60 之间。")
    if not 0 <= delay_minutes <= 24 * 60:
        raise VideoPreparerError("harmonized_delay_minutes 必须在 0 到 1440 之间。")
    default_footer_lines = DEFAULT_TIMESTAMP_FOOTER.splitlines()
    custom_footer = any(
        key.startswith("timestamp_footer_line_") for key in values
    )
    footer_lines = [
        values.get(
            f"timestamp_footer_line_{index}",
            (
                ""
                if custom_footer
                else default_footer_lines[index - 1]
                if index <= len(default_footer_lines)
                else ""
            ),
        ).strip()
        for index in range(1, 6)
    ]
    output_folder_name = values.get("output_folder_name", "AutoFlow输出").strip()
    if not output_folder_name or any(character in output_folder_name for character in '<>:"/\\|?*'):
        raise VideoPreparerError("output_folder_name 不是有效的 Windows 文件夹名称。")
    default_output_layout = values.get("default_output_layout", "ask").strip().casefold()
    if default_output_layout != "ask":
        default_output_layout = normalize_layout(default_output_layout)
    preferred_formats = tuple(
        item if item.startswith(".") else f".{item}"
        for item in (
            part.strip().casefold()
            for part in values.get("preferred_audio_formats", "wav,flac,ape,m4a,mp3").split(",")
        )
        if item
    )
    bonus_policy = values.get("bonus_policy", "ask").strip().casefold()
    if bonus_policy not in {"ask", "include", "exclude"}:
        raise VideoPreparerError("bonus_policy 必须是 ask、include 或 exclude。")
    background_policy = values.get("background_policy", "auto").strip().casefold()
    if background_policy not in {"auto", "black"}:
        raise VideoPreparerError("background_policy 必须是 auto 或 black。")
    return AppConfig(
        asmr_root=asmr_root,
        harmonized_volume_db=-abs(reduction),
        harmonized_delay_seconds=round(delay_minutes * 60),
        timestamp_footer="\n".join(line for line in footer_lines if line),
        output_folder_name=output_folder_name,
        default_output_layout=default_output_layout,
        preferred_audio_formats=preferred_formats,
        bonus_policy=bonus_policy,
        background_policy=background_policy,
    )


def clean_user_path(value: str) -> Path:
    text = value.strip().strip('"').strip("'")
    if not text:
        raise VideoPreparerError("没有输入作品文件夹路径。")
    return Path(text).expanduser().resolve()


def find_tool_paths(config: AppConfig) -> ToolPaths:
    configured = (
        os.environ.get("ASMR_DUBBER_ROOT", "").strip()
        or os.environ.get("ASMR_NEXT_ROOT", "").strip()
    )
    candidates = []
    if config.asmr_root is not None:
        candidates.append(config.asmr_root)
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        (
            TOOL_ROOT.parent / "ASMR-Dubber",
            TOOL_ROOT.parent / "asmr-next",
        )
    )

    asmr_root = next((path.resolve() for path in candidates if path.is_dir()), None)
    if asmr_root is None:
        raise VideoPreparerError(
            "找不到 ASMR Dubber。请在 settings.txt 中填写 asmr_dubber_path，"
            "或设置环境变量 ASMR_DUBBER_ROOT。"
        )

    asmr_home = asmr_root / ".asmr-dubber"
    python_candidates = (
        asmr_home / "venv" / "Scripts" / "python.exe",
        asmr_root / ".venv" / "Scripts" / "python.exe",
    )
    python = next((path for path in python_candidates if path.is_file()), None)
    if python is None:
        raise VideoPreparerError("ASMR Dubber 尚未安装完整运行环境，找不到便携 Python。")

    ffmpeg_root = asmr_home / "runtimes" / "ffmpeg-shared"
    ffmpeg = next(iter(sorted(ffmpeg_root.rglob("ffmpeg.exe"))), None)
    ffprobe = next(iter(sorted(ffmpeg_root.rglob("ffprobe.exe"))), None)
    if ffmpeg is None or ffprobe is None:
        raise VideoPreparerError("ASMR Dubber 的 FFmpeg/FFprobe 不完整，请先修复其安装。")

    # AutoFlow calls a few supported ASMR Dubber Python APIs directly (script
    # import and voice-reference extraction). The normal launcher sets these
    # variables in PowerShell; set the same portable paths here so a fresh
    # Windows machine does not need a system-wide FFmpeg installation.
    os.environ["ASMR_DUBBER_HOME"] = str(asmr_home)
    os.environ["ASMR_DUBBER_FFMPEG"] = str(ffmpeg)
    current_path = os.environ.get("PATH", "")
    ffmpeg_bin = str(ffmpeg.parent)
    if ffmpeg_bin.casefold() not in {
        item.casefold() for item in current_path.split(os.pathsep) if item
    }:
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + current_path

    cli_script = asmr_root / "scripts" / "windows" / "run-cli.ps1"
    launcher = asmr_root / "ASMR-Dubber.exe"
    if not cli_script.is_file() or not launcher.is_file():
        raise VideoPreparerError("ASMR Dubber 缺少命令行脚本或启动程序。")

    powershell_path = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell_path:
        raise VideoPreparerError("找不到 PowerShell。Windows 自带的 PowerShell 5.1 即可。")

    encoder_result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    encoder_text = encoder_result.stdout + encoder_result.stderr
    if re.search(r"\blibx264\b", encoder_text):
        video_encoder_options = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20")
    elif re.search(r"\blibopenh264\b", encoder_text):
        video_encoder_options = (
            "-c:v",
            "libopenh264",
            "-rc_mode",
            "quality",
            "-q:v",
            "20",
        )
    elif re.search(r"\bmpeg4\b", encoder_text):
        video_encoder_options = ("-c:v", "mpeg4", "-q:v", "2")
    else:
        raise VideoPreparerError("FFmpeg 没有可用的软件视频编码器。")
    video_encoder_options = (*video_encoder_options, *PERIODIC_KEYFRAME_OPTIONS)

    return ToolPaths(
        asmr_root=asmr_root,
        asmr_home=asmr_home,
        python=python,
        cli_script=cli_script,
        launcher=launcher,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        powershell=powershell_path,
        video_encoder_options=video_encoder_options,
    )


def validate_asmr_version() -> str:
    try:
        from asmr_dubber import __version__
    except Exception as exc:
        raise VideoPreparerError(f"无法读取 ASMR Dubber 版本：{exc}") from exc
    version = str(__version__)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is not None and tuple(map(int, match.groups())) < (0, 7, 1):
        raise VideoPreparerError(
            f"ASMR Dubber {version} 太旧；AutoFlow 需要 0.7.1 或后续兼容版本。"
        )
    log_event(f"检测到 ASMR Dubber {version}")
    return version


def discover_audio(folder: Path) -> list[AudioSource]:
    matches: list[AudioSource] = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        match = NUMBERED_NAME.match(path.stem)
        if match is None:
            continue
        title = match.group(2).strip(" ._-、") or path.stem.strip()
        stat = path.stat()
        matches.append(
            AudioSource(
                order=int(match.group(1)),
                path=path.resolve(),
                title_ja=title,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )

    matches.sort(key=lambda item: (item.order, item.path.name.casefold()))
    if not matches:
        raise VideoPreparerError(
            "没有找到以数字开头的音频。示例：1 开场.mp3、2 催眠.flac、10 结束.m4a"
        )

    duplicates: dict[int, list[str]] = {}
    for item in matches:
        duplicates.setdefault(item.order, []).append(item.path.name)
    repeated = {number: names for number, names in duplicates.items() if len(names) > 1}
    if repeated:
        print("\n警告：以下编号重复；同编号将按文件名排序：")
        for number, names in repeated.items():
            print(f"  {number}: {', '.join(names)}")
    return matches


def discover_background(folder: Path) -> Path | None:
    candidates = [
        path
        for path in folder.iterdir()
        if path.is_file()
        and path.stem.casefold() == "null"
        and path.suffix.casefold() in IMAGE_EXTENSIONS
    ]
    if not candidates:
        return None
    preference = {extension: index for index, extension in enumerate(IMAGE_EXTENSIONS)}
    candidates.sort(key=lambda path: (preference[path.suffix.casefold()], path.name.casefold()))
    if len(candidates) > 1:
        print(f"警告：发现多张 null 图片，将使用：{candidates[0].name}")
    return candidates[0].resolve()


def source_from_candidate(index: int, candidate: TrackCandidate) -> AudioSource:
    stat = candidate.path.stat()
    transcript = candidate.transcript
    return AudioSource(
        order=index,
        path=candidate.path.resolve(),
        title_ja=candidate.title,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        relative_path=candidate.relative_path,
        category=candidate.category,
        transcript_path=transcript.path.resolve() if transcript else None,
        transcript_language=transcript.language if transcript else None,
        transcript_timed=transcript.timed if transcript else False,
        source_language=candidate.language,
    )


def _edition_rank(edition: Edition, config: AppConfig) -> tuple[int, int, Any]:
    extension = edition.extension.casefold()
    try:
        format_rank = len(config.preferred_audio_formats) - config.preferred_audio_formats.index(
            extension
        )
    except ValueError:
        format_rank = 0
    return edition.score, format_rank, natural_key(edition.label)


def _all_scan_tracks(scan: ScanResult) -> list[TrackCandidate]:
    unique: dict[Path, TrackCandidate] = {}
    for edition in scan.editions:
        for track in edition.all_tracks:
            unique.setdefault(track.path.resolve(), track)
    return sorted(
        unique.values(),
        key=lambda item: (item.order_key, natural_key(item.relative_path)),
    )


def _parse_track_selection(value: str, maximum: int) -> list[int]:
    selected: set[int] = set()
    for raw_part in value.replace("，", ",").split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", part)
        if match:
            start, end = map(int, match.groups())
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
            continue
        if not part.isdigit():
            raise VideoPreparerError(f"无法识别音轨选择：{part}")
        selected.add(int(part))
    invalid = sorted(index for index in selected if not 1 <= index <= maximum)
    if invalid:
        raise VideoPreparerError("音轨编号超出范围：" + "、".join(map(str, invalid)))
    return sorted(selected)


def category_label(value: str) -> str:
    return {
        "main": "正文",
        "bonus": "特典",
        "sample": "样本",
        "freetalk": "Free Talk",
        "alarm": "闹钟/提示音",
    }.get(value, value)


def choose_tracks(
    scan: ScanResult,
    config: AppConfig,
    *,
    edition_argument: str | None = None,
    include_bonus: bool = False,
) -> tuple[str, list[AudioSource], dict[str, Any]]:
    if not scan.editions:
        raise VideoPreparerError("没有找到可处理的音频文件。")

    editions = sorted(
        scan.editions,
        key=lambda item: _edition_rank(item, config),
        reverse=True,
    )
    recommended = editions[0]
    chosen: Edition | None = None

    if edition_argument:
        value = edition_argument.strip()
        if value.isdigit() and 1 <= int(value) <= len(editions):
            chosen = editions[int(value) - 1]
        else:
            chosen = next((item for item in editions if item.id == value), None)
        if chosen is None:
            raise VideoPreparerError(f"找不到指定版本：{edition_argument}")
    elif len(editions) == 1:
        chosen = editions[0]
        print(f"\n只发现一组可处理音轨：{chosen.label}")
    elif recommended.legacy_compatible:
        chosen = recommended
        print("\n检测到传统的根目录数字音轨，将直接沿用原来的处理顺序。")
    else:
        print("\n检测到多组音频，请选择要处理的版本：")
        for index, edition in enumerate(editions, start=1):
            marker = "（推荐）" if edition.id == recommended.id else ""
            optional_text = (
                f"，另有 {len(edition.optional_tracks)} 个特典/样本"
                if edition.optional_tracks
                else ""
            )
            print(
                f"  {index}. {edition.label}：{len(edition.tracks)} 轨{optional_text}{marker}"
            )
        print("  M. 手动逐条选择")
        answer = input(f"输入编号；直接按 Enter 使用推荐的 {1}：").strip().casefold()
        if not answer:
            chosen = recommended
        elif answer == "m":
            all_tracks = _all_scan_tracks(scan)
            print("\n全部音频：")
            for index, track in enumerate(all_tracks, start=1):
                category = "" if track.category == "main" else f" [{category_label(track.category)}]"
                print(f"  {index:>3}. {track.relative_path}{category}")
            raw = input("输入要处理的编号，例如 1,3-6：").strip()
            indexes = _parse_track_selection(raw, len(all_tracks))
            if not indexes:
                raise VideoPreparerError("没有选择任何音轨。")
            selected_candidates = [all_tracks[index - 1] for index in indexes]
            sources = [
                source_from_candidate(index, item)
                for index, item in enumerate(selected_candidates, start=1)
            ]
            return (
                "自定义音轨",
                sources,
                {
                    "edition_id": "custom",
                    "edition_label": "自定义音轨",
                    "manual": True,
                    "included_optional": any(item.is_optional for item in selected_candidates),
                },
            )
        elif answer.isdigit() and 1 <= int(answer) <= len(editions):
            chosen = editions[int(answer) - 1]
        else:
            raise VideoPreparerError("版本选择无效。")

    assert chosen is not None
    candidates = list(chosen.tracks)
    chosen_paths = {item.path.resolve() for item in candidates}
    # Optional material is often placed in a separate ``特典``/``Bonus``
    # directory, so it does not share the main edition's exact grouping key.
    # Offer compatible optional files from the whole scan as well, while
    # deduplicating WAV/MP3 mirrors of the same title.
    global_optional = [
        item
        for item in _all_scan_tracks(scan)
        if item.is_optional
        and item.path.resolve() not in chosen_paths
        and item.language == chosen.language
        and item.orientation == chosen.orientation
        and item.mix_variant in {chosen.mix_variant, "standard"}
    ]
    if chosen.extension != ".mixed":
        same_format = [item for item in global_optional if item.extension == chosen.extension]
        if same_format:
            global_optional = same_format
    optional_pool = [*chosen.optional_tracks, *global_optional]
    unique_optional: dict[Path, TrackCandidate] = {}
    for item in optional_pool:
        unique_optional.setdefault(item.path.resolve(), item)

    def optional_format_rank(candidate: TrackCandidate) -> tuple[int, str]:
        try:
            rank = config.preferred_audio_formats.index(candidate.extension)
        except ValueError:
            rank = len(config.preferred_audio_formats) + 1
        return rank, candidate.relative_path.casefold()

    optional_by_title: dict[tuple[str, int, int, str, str], TrackCandidate] = {}
    for item in unique_optional.values():
        section, number, suffix, _ = item.order_key
        key = (item.category, section, number, suffix, item.title.casefold().strip())
        current = optional_by_title.get(key)
        if current is None:
            optional_by_title[key] = item
            continue
        if optional_format_rank(item) < optional_format_rank(current):
            optional_by_title[key] = item
    optional_pool = list(optional_by_title.values())
    include_optional = include_bonus
    if optional_pool and not include_optional:
        if config.bonus_policy == "include":
            include_optional = True
        elif config.bonus_policy == "ask" and edition_argument is None:
            print("\n这一版本还包含：")
            counts: dict[str, int] = {}
            for item in optional_pool:
                counts[item.category] = counts.get(item.category, 0) + 1
            print(
                "  "
                + "，".join(
                    f"{category_label(name)} {count} 轨" for name, count in counts.items()
                )
            )
            include_optional = input("是否一并处理？输入 Y 确认：").strip().casefold() == "y"
    if include_optional:
        candidates.extend(optional_pool)
        candidates.sort(key=lambda item: (item.order_key, natural_key(item.relative_path)))
    sources = [
        source_from_candidate(index, item)
        for index, item in enumerate(candidates, start=1)
    ]
    return (
        chosen.label,
        sources,
        {
            "edition_id": chosen.id,
            "edition_label": chosen.label,
            "manual": False,
            "included_optional": include_optional,
            "directory": chosen.directory,
            "extension": chosen.extension,
            "language": chosen.language,
            "mix_variant": chosen.mix_variant,
            "orientation": chosen.orientation,
        },
    )


def smart_background(scan: ScanResult, config: AppConfig) -> Path | None:
    if config.background_policy == "black" or not scan.images:
        return None
    return scan.images[0].resolve()


def safe_filename_component(value: str, *, fallback: str = "未命名", limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" ._")
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = fallback
    return text[:limit].rstrip(" ._") or fallback


def ask_output_layout(config: AppConfig, argument: str | None = None) -> str:
    if argument:
        return normalize_layout(argument)
    if config.default_output_layout != "ask":
        return normalize_layout(config.default_output_layout)
    print("\n请选择成品组织方式：")
    print("  1. 合并成一部")
    print("  2. 每条音轨分别输出（不拼接）")
    print("  3. 分轨输出 + 合并版")
    while True:
        answer = input("输入 1、2 或 3：").strip()
        if answer == "1":
            return LAYOUT_MERGED
        if answer == "2":
            return LAYOUT_SEPARATE
        if answer == "3":
            return LAYOUT_BOTH
        print("输入无效，请重新选择。")


def file_stat_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "missing": True}
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def fingerprint(audio: Iterable[AudioSource], background: Path | None) -> dict[str, Any]:
    image_info: dict[str, Any] | None = None
    if background is not None:
        stat = background.stat()
        image_info = {
            "path": str(background),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return {
        "audio": [
            {
                "order": item.order,
                "path": str(item.path),
                "relative_path": item.relative_path,
                "size": item.size,
                "mtime_ns": item.mtime_ns,
                "category": item.category,
                "transcript": file_stat_payload(item.transcript_path),
                "transcript_language": item.transcript_language,
                "transcript_timed": item.transcript_timed,
                "source_language": item.source_language,
            }
            for item in audio
        ],
        "background": image_info,
    }


def task_key(folder: Path) -> str:
    normalized = os.path.normcase(str(folder.resolve())).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:20]


def state_path(folder: Path) -> Path:
    return STATE_ROOT / f"{task_key(folder)}.json"


def workspace_path(folder: Path) -> Path:
    return WORK_ROOT / task_key(folder)


def planned_job_key(source_folder: Path, plan_id: str, job_id: str) -> str:
    raw = "\n".join(
        (
            os.path.normcase(str(source_folder.resolve())),
            plan_id,
            job_id,
        )
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def planned_state_path(source_folder: Path, plan_id: str, job_id: str) -> Path:
    return STATE_ROOT / f"smart-{planned_job_key(source_folder, plan_id, job_id)}.json"


def planned_workspace_path(source_folder: Path, plan_id: str, job_id: str) -> Path:
    return WORK_ROOT / f"smart-{planned_job_key(source_folder, plan_id, job_id)}"


def plan_identity(
    source_folder: Path,
    *,
    mode: str,
    layout: str,
    edition: dict[str, Any],
    sources: list[AudioSource],
    output_root: Path | None = None,
    background: Path | None = None,
) -> str:
    payload = {
        "source_folder": os.path.normcase(str(source_folder.resolve())),
        "mode": normalize_mode(mode),
        "layout": normalize_layout(layout),
        "edition": edition,
        "output_root": str(output_root.resolve()) if output_root else None,
        "background": file_stat_payload(background),
        "tracks": [
            {
                "path": os.path.normcase(str(item.path.resolve())),
                "size": item.size,
                "mtime_ns": item.mtime_ns,
                "transcript": file_stat_payload(item.transcript_path),
                "source_language": item.source_language,
            }
            for item in sources
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def plan_metadata_path(plan_id: str) -> Path:
    return STATE_ROOT / f"smart-plan-{plan_id}.json"


def load_plan_metadata(plan_id: str) -> dict[str, Any]:
    path = plan_metadata_path(plan_id)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if payload.get("schema") == 1 else {}


def save_plan_metadata(plan_id: str, payload: dict[str, Any]) -> None:
    stored = {"schema": 1, **payload}
    stored["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(
        plan_metadata_path(plan_id),
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_plan_manifest(
    destination: Path,
    *,
    source_folder: Path,
    output_folder: Path,
    mode: str,
    layout: str,
    edition: dict[str, Any],
    sources: list[AudioSource],
    background: Path | None,
    plan_id: str | None = None,
    jobs: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "plan_id": plan_id,
        "source_folder": str(source_folder),
        "output_folder": str(output_folder),
        "mode": normalize_mode(mode),
        "layout": normalize_layout(layout),
        "edition": edition,
        "background": str(background) if background else None,
        "tracks": [
            {
                "index": index,
                "path": str(item.path),
                "relative_path": item.relative_path or item.path.name,
                "title": item.title_ja,
                "category": item.category,
                "transcript": str(item.transcript_path) if item.transcript_path else None,
                "transcript_language": item.transcript_language,
                "transcript_timed": item.transcript_timed,
                "source_language": item.source_language,
            }
            for index, item in enumerate(sources, start=1)
        ],
        "jobs": list(jobs or ()),
    }
    atomic_write_text(
        destination,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise VideoPreparerError(f"任务状态文件损坏：{path}: {exc}") from exc
    if payload.get("schema") != 1:
        raise VideoPreparerError(f"不支持的任务状态版本：{path}")
    payload["mode"] = normalize_mode(payload.get("mode"))
    payload.setdefault(
        "harmonized_volume_db", -DEFAULT_HARMONIZED_VOLUME_REDUCTION_DB
    )
    payload.setdefault(
        "harmonized_delay_seconds", round(DEFAULT_HARMONIZED_DELAY_MINUTES * 60)
    )
    return payload


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def status_at_least(state: dict[str, Any], status: str) -> bool:
    return STATUS_ORDER.get(str(state.get("status", "")), 0) >= STATUS_ORDER[status]


def safe_reset_workspace(workspace: Path) -> None:
    root = WORK_ROOT.resolve()
    target = workspace.resolve()
    if target.parent != root:
        raise VideoPreparerError(f"拒绝清理非任务目录：{target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the current FFmpeg/ASMR CLI command and its child processes."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_process(arguments: list[str], *, cwd: Path | None = None) -> None:
    log_event("运行命令：" + " ".join(str(item) for item in arguments))
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise VideoPreparerError(f"无法启动命令：{arguments[0]}: {exc}") from exc
    assert process.stdout is not None
    try:
        for line in process.stdout:
            print(line, end="")
            append_log(line)
        result_code = process.wait()
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    if result_code != 0:
        raise VideoPreparerError(
            f"命令执行失败（退出码 {result_code}）：{Path(arguments[0]).name}"
        )


def run_process_captured(arguments: list[str], *, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.update({"COLUMNS": "10000", "NO_COLOR": "1", "TERM": "dumb"})
    try:
        log_event("运行命令：" + " ".join(str(item) for item in arguments))
        result = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise VideoPreparerError(f"无法启动命令：{arguments[0]}: {exc}") from exc
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        append_log(result.stdout)
    if result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )
        append_log(result.stderr)
    if result.returncode != 0:
        raise VideoPreparerError(
            f"命令执行失败（退出码 {result.returncode}）：{Path(arguments[0]).name}"
        )
    return "\n".join(part for part in (result.stdout, result.stderr) if part)


def run_ffmpeg(paths: ToolPaths, arguments: list[str], *, cwd: Path | None = None) -> None:
    command = [
        str(paths.ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-y",
        *arguments,
    ]
    run_process(command, cwd=cwd)


def ffprobe_json(paths: ToolPaths, media: Path, entries: str) -> dict[str, Any]:
    command = [
        str(paths.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        entries,
        "-of",
        "json",
        str(media),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise VideoPreparerError(f"无法运行 FFprobe：{exc}") from exc
    if result.returncode != 0:
        raise VideoPreparerError(f"FFprobe 无法读取 {media.name}：{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VideoPreparerError(f"FFprobe 返回了无效结果：{media}") from exc


def audio_duration_samples(paths: ToolPaths, media: Path) -> int:
    payload = ffprobe_json(
        paths,
        media,
        "stream=sample_rate,duration_ts,time_base:format=duration",
    )
    streams = payload.get("streams") or []
    if not streams:
        raise VideoPreparerError(f"文件没有可用音轨：{media}")
    stream = streams[0]
    try:
        sample_rate = int(stream["sample_rate"])
        duration_ts = int(stream["duration_ts"])
        time_base = Fraction(str(stream["time_base"]))
        duration = Fraction(duration_ts) * time_base
        samples = duration * SAMPLE_RATE
        if samples.denominator != 1:
            samples = Fraction(round(float(samples)), 1)
        if sample_rate <= 0 or samples <= 0:
            raise ValueError
        return int(samples)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        try:
            duration = Fraction(str(payload["format"]["duration"]))
            samples = round(float(duration) * SAMPLE_RATE)
            if samples <= 0:
                raise ValueError
            return samples
        except (KeyError, TypeError, ValueError) as fallback_exc:
            raise VideoPreparerError(f"无法取得准确音频时长：{media}") from fallback_exc


def normalize_and_concat(
    paths: ToolPaths,
    sources: list[AudioSource],
    workspace: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    segments_dir = workspace / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    timeline: list[dict[str, Any]] = []
    cumulative_samples = 0

    print("\n[1/5] 统一音频规格并计算实际时间轴")
    for index, source in enumerate(sources, start=1):
        output = segments_dir / f"seg_{index:06d}.flac"
        print(f"  [{index}/{len(sources)}] {source.path.name}")
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "warning",
                "-i",
                str(source.path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                f"aresample={SAMPLE_RATE}:async=0",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-sample_fmt",
                "s16",
                "-c:a",
                "flac",
                "-compression_level",
                "0",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                str(output),
            ],
        )
        samples = audio_duration_samples(paths, output)
        timeline.append(
            {
                "order": source.order,
                "source": str(source.path),
                "filename": source.path.name,
                "relative_path": source.relative_path or source.path.name,
                "title_ja": source.title_ja,
                "category": source.category,
                "transcript": str(source.transcript_path) if source.transcript_path else None,
                "transcript_language": source.transcript_language,
                "transcript_timed": source.transcript_timed,
                "source_language": source.source_language,
                "normalized": str(output),
                "start_samples": cumulative_samples,
                "duration_samples": samples,
            }
        )
        cumulative_samples += samples

    concat_file = workspace / "concat.ffconcat"
    concat_lines = ["ffconcat version 1.0"]
    concat_lines.extend(
        f"file 'segments/seg_{index:06d}.flac'" for index in range(1, len(sources) + 1)
    )
    concat_file.write_text("\n".join(concat_lines) + "\n", encoding="ascii")

    master = workspace / "master.flac"
    print("  正在拼接无损母带……")
    run_ffmpeg(
        paths,
        [
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            concat_file.name,
            "-map",
            "0:a:0",
            "-af",
            "asetpts=N/SR/TB",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "flac",
            "-compression_level",
            "0",
            str(master),
        ],
        cwd=workspace,
    )
    master_samples = audio_duration_samples(paths, master)
    if abs(master_samples - cumulative_samples) > 1:
        raise VideoPreparerError(
            "拼接后的采样数与各小音频之和不一致，已停止，避免生成错误时间轴。"
        )
    return master, timeline


def partial_output_path(destination: Path) -> Path:
    return destination.with_name(f".{destination.stem}.partial.{uuid.uuid4().hex}{destination.suffix}")


def background_input(background: Path | None) -> list[str]:
    if background is not None:
        return ["-i", str(background)]
    return [
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={VIDEO_SIZE}:r=1:d=1",
    ]


def render_static_video(
    paths: ToolPaths,
    audio_source: Path,
    background: Path | None,
    destination: Path,
    *,
    lead_seconds: int = 0,
    volume_db: float = 0.0,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_output_path(destination)
    total_samples = audio_duration_samples(paths, audio_source) + lead_seconds * SAMPLE_RATE
    duration_text = f"{total_samples / SAMPLE_RATE:.6f}"
    audio_filters = [
        f"aresample={SAMPLE_RATE}:async=0",
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo",
    ]
    if volume_db:
        audio_filters.append(f"volume={volume_db:g}dB")
    if lead_seconds:
        audio_filters.append(f"adelay={lead_seconds * 1000}:all=1")
    audio_filters.append("asetpts=N/SR/TB")

    # Scale the source picture once, then loop that prepared 1080p frame in
    # memory. This avoids decoding an 8K JPEG again for every video frame.
    video_filter = (
        f"[0:v:0]scale={VIDEO_FILTER_SIZE}:force_original_aspect_ratio=decrease:"
        f"flags=lanczos,pad={VIDEO_FILTER_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,format=yuv420p,loop=loop=-1:size=1:start=0,"
        f"setpts=N/{VIDEO_FPS}/TB,trim=duration={duration_text}[v];"
        f"[1:a:0]{','.join(audio_filters)}[a]"
    )
    try:
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "warning",
                "-stats",
                "-stats_period",
                "5",
                *background_input(background),
                "-i",
                str(audio_source),
                "-filter_complex",
                video_filter,
                "-map",
                "[v]",
                "-map",
                "[a]",
                *paths.video_encoder_options,
                "-r",
                str(VIDEO_FPS),
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-b:a",
                "256k",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-t",
                duration_text,
                "-shortest",
                "-movflags",
                "+faststart",
                "-metadata",
                f"title={destination.stem}",
                str(partial),
            ],
        )
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def render_delayed_existing_video(
    paths: ToolPaths,
    source: Path,
    destination: Path,
    *,
    lead_seconds: int,
    subtitle_file: Path,
    volume_db: float = 0.0,
    audio_source: Path | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_output_path(destination)
    audio_filters = [
        f"aresample={SAMPLE_RATE}:async=0",
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo",
    ]
    if volume_db:
        audio_filters.append(f"volume={volume_db:g}dB")
    audio_filters.extend(
        (f"adelay={lead_seconds * 1000}:all=1", "asetpts=N/SR/TB")
    )
    audio_input_index = 1 if audio_source is not None else 0
    subtitle_input_index = 2 if audio_source is not None else 1
    filter_complex = (
        f"[0:v:0]tpad=start_mode=clone:start_duration={lead_seconds},"
        f"scale={VIDEO_FILTER_SIZE}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_FILTER_SIZE}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={VIDEO_FPS},format=yuv420p,setpts=PTS-STARTPTS[v];"
        f"[{audio_input_index}:a:0]{','.join(audio_filters)}[a]"
    )
    input_arguments = ["-i", str(source)]
    if audio_source is not None:
        input_arguments.extend(("-i", str(audio_source)))
    input_arguments.extend(("-f", "srt", "-i", str(subtitle_file)))
    try:
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "warning",
                "-stats",
                "-stats_period",
                "5",
                *input_arguments,
                "-filter_complex",
                filter_complex,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-map",
                f"{subtitle_input_index}:s:0",
                *paths.video_encoder_options,
                "-r",
                str(VIDEO_FPS),
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-b:a",
                "256k",
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=zho",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-metadata",
                "title=双语版",
                str(partial),
            ],
        )
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_output_path(destination)
    try:
        with source.open("rb") as reader, partial.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=16 * 1024 * 1024)
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def run_asmr_cli(paths: ToolPaths, *arguments: str) -> None:
    command = [
        paths.powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(paths.cli_script),
        *arguments,
    ]
    run_process(command, cwd=paths.asmr_root)


def run_asmr_cli_captured(paths: ToolPaths, *arguments: str) -> str:
    command = [
        paths.powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(paths.cli_script),
        *arguments,
    ]
    return run_process_captured(command, cwd=paths.asmr_root)


def configured_projects_root(paths: ToolPaths) -> Path:
    try:
        from asmr_dubber.user_settings import load_user_settings

        value = str(load_user_settings().projects_root or "").strip()
    except Exception as exc:
        raise VideoPreparerError(f"无法读取 ASMR Dubber 用户设置：{exc}") from exc
    return Path(value).expanduser().resolve() if value else (paths.asmr_home / "projects")


def known_project_manifests(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {path.resolve() for path in root.rglob("project.json") if path.is_file()}


def create_asmr_project(
    paths: ToolPaths,
    video: Path,
    *,
    source_language: str = "ja",
) -> Path:
    projects_root = configured_projects_root(paths)
    before = known_project_manifests(projects_root)
    started_ns = time.time_ns()
    output = run_asmr_cli_captured(
        paths,
        "create",
        str(video),
        "--projects-root",
        str(projects_root),
        "--source-language",
        "en" if source_language == "en" else "ja",
    )
    ansi_escape = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    for raw_line in reversed(output.splitlines()):
        clean_line = ansi_escape.sub("", raw_line).strip().strip('"')
        if not clean_line.casefold().endswith("project.json"):
            continue
        candidate = Path(clean_line).expanduser().resolve()
        try:
            candidate.relative_to(projects_root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate

    after = known_project_manifests(projects_root)
    created = list(after - before)
    if not created:
        created = [
            path
            for path in after
            if path.stat().st_mtime_ns >= started_ns - 5_000_000_000
        ]
    if not created:
        raise VideoPreparerError("ASMR Dubber 已返回成功，但没有找到新项目的 project.json。")
    created.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    return created[0]


def read_project(project_json: Path) -> dict[str, Any]:
    try:
        return json.loads(project_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise VideoPreparerError(f"无法读取 ASMR Dubber 项目：{project_json}: {exc}") from exc


def project_asset(project_json: Path, stored: Any) -> Path | None:
    text = str(stored or "").strip()
    if not text:
        return None
    candidate = (project_json.parent / text).resolve()
    try:
        candidate.relative_to(project_json.parent.resolve())
    except ValueError as exc:
        raise VideoPreparerError(f"项目输出路径越界：{text}") from exc
    return candidate if candidate.is_file() else None


def source_language_for_sources(sources: Sequence[AudioSource]) -> str:
    """Choose the only ASR-compatible source language represented by a job.

    ASMR Dubber currently accepts Japanese and English at project creation. A
    Chinese timed script is imported later and changes the project language,
    so Chinese-labelled files intentionally fall back to Japanese here.
    """

    languages = {str(item.source_language).casefold() for item in sources}
    return "en" if languages and languages == {"en"} else "ja"


def ensure_autoflow_project_outputs(project_json: Path) -> None:
    """Ensure an AutoFlow project can produce the promised bilingual mix."""

    try:
        from asmr_dubber.models import load_project, save_project

        project, project_dir = load_project(project_json)
        if project.settings.mix_output_mode == "stem":
            project.settings.mix_output_mode = "both"
            save_project(project, project_dir)
    except Exception as exc:
        raise VideoPreparerError(f"无法准备 ASMR Dubber 项目的混合输出设置：{exc}") from exc


def launch_asmr_ui(paths: ToolPaths, project_json: Path) -> None:
    try:
        subprocess.run(
            [
                paths.powershell,
                "-NoProfile",
                "-Command",
                "Set-Clipboard -Value $args[0]",
                str(project_json),
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            [str(paths.launcher)],
            cwd=paths.asmr_root,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise VideoPreparerError(f"无法启动 ASMR Dubber 网页：{exc}") from exc


def project_reference_id(project_json: Path) -> str:
    project = read_project(project_json)
    settings = project.get("settings") or {}
    return str(settings.get("tts_reference_sentence_id") or "").strip()


def project_has_external_reference(project_json: Path) -> bool:
    """Return whether the current 0.7.x project already has a usable reference."""

    project = read_project(project_json)
    settings = project.get("settings") or {}
    source = str(settings.get("tts_reference_source") or "project_sentence").strip()
    if source != "external":
        return False
    audio = Path(str(settings.get("tts_external_reference_audio") or "")).expanduser()
    return audio.is_file()


def wait_for_reference(paths: ToolPaths, project_json: Path) -> bool:
    print("\n[4/5] 等你在网页中选择统一音色参考")
    print(f"  项目：{project_json}")
    print("  项目路径已复制到剪贴板。网页会预选最近项目；请点击“打开项目”。")
    print("  选择清晰片段后，必须点击“设为项目音色参考”。")
    print("  如果还修改了表格，请同时点击“保存校对表格”。")
    print("  保存参考后程序会自动继续；5 分钟内未选择则使用默认参考音频。")
    launch_asmr_ui(paths, project_json)

    deadline = time.monotonic() + REFERENCE_SELECTION_TIMEOUT_SECONDS
    next_notice = time.monotonic() + 60
    while True:
        reference_id = project_reference_id(project_json)
        if reference_id:
            print(f"已检测到统一参考：{reference_id}")
            return True
        if project_has_external_reference(project_json):
            print("已检测到 ASMR Dubber 设置中的外部参考音频。")
            return True
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            print("5 分钟内未检测到手动选择，将使用 ASMR Dubber 推荐的默认参考音频。")
            return True
        if now >= next_notice:
            print(f"  仍在等待参考音频，剩余约 {max(1, int(remaining // 60) + 1)} 分钟……")
            next_notice = now + 60
        time.sleep(min(2.0, remaining))


def project_source_language(project_json: Path) -> str:
    project = read_project(project_json)
    return str(project.get("source_language") or "ja")


def extract_shared_reference(project_json: Path, destination: Path) -> dict[str, str]:
    """把首个分轨项目的统一参考固化为后续项目可复用的外部参考。"""

    try:
        from asmr_dubber.models import load_project, save_project
        from asmr_dubber.voice_reference import prepare_voice_reference, shared_reference_sentence

        project, project_dir = load_project(project_json)
        sentence = shared_reference_sentence(project)
        source = (project_dir / project.source.path).resolve()
        reference = prepare_voice_reference(project, project_dir, source, sentence)
        save_project(project, project_dir)
    except Exception as exc:
        raise VideoPreparerError(f"无法固化分轨共用参考音频：{exc}") from exc

    atomic_copy(reference.path, destination)
    return {
        "audio": str(destination.resolve()),
        "text": reference.text,
        "language": reference.language,
    }


def apply_shared_reference(project_json: Path, reference: dict[str, str]) -> None:
    audio = Path(str(reference.get("audio") or "")).expanduser().resolve()
    if not audio.is_file():
        raise VideoPreparerError(f"分轨共用参考音频已经不存在：{audio}")
    try:
        from asmr_dubber.models import load_project, save_project

        project, project_dir = load_project(project_json)
        project.settings.tts_reference_source = "external"
        project.settings.tts_external_reference_audio = str(audio)
        project.settings.tts_external_reference_text = str(reference.get("text") or "")
        language = str(reference.get("language") or "auto")
        project.settings.tts_external_reference_language = (
            language if language in {"ja", "en", "zh"} else "auto"
        )
        # IndexTTS2 有独立的音色参考选择；情绪仍可跟随当前句。
        project.settings.tts_index_speaker_source = "external"
        save_project(project, project_dir)
    except Exception as exc:
        raise VideoPreparerError(f"无法给分轨项目设置共用参考音频：{exc}") from exc


def _qwen_script_alignment_available(paths: ToolPaths) -> bool:
    model_root = paths.asmr_home / "models"
    if not model_root.is_dir():
        return False
    return any(
        "qwen3-forcedaligner" in path.as_posix().casefold()
        for path in model_root.rglob("config.json")
    )


def _load_transcript_sentences(
    path: Path,
    *,
    language: str,
    duration_seconds: float,
) -> Any:
    try:
        from asmr_dubber.transcript_import import parse_transcript

        return parse_transcript(
            duration_seconds=duration_seconds,
            path=path,
            language=language,
        )
    except Exception as exc:
        raise VideoPreparerError(f"无法解析台本/字幕 {path.name}：{exc}") from exc


def import_available_source_transcript(
    paths: ToolPaths,
    project_json: Path,
    timeline: list[dict[str, Any]],
) -> dict[str, Any] | None:
    usable = [
        item
        for item in timeline
        if item.get("transcript")
        and str(item.get("transcript_language") or "") in {"ja", "en", "zh"}
        and Path(str(item["transcript"])).suffix.casefold() != ".pdf"
    ]
    if not usable:
        return None

    # 单轨可以直接使用 ASMR Dubber 的完整台本导入流程，包括纯文本对齐。
    if len(timeline) == 1 and len(usable) == 1:
        item = usable[0]
        transcript = Path(str(item["transcript"])).resolve()
        language = str(item["transcript_language"])
        if language == "zh" and bool(item.get("transcript_timed")):
            # 中文时间轴需要先保留日文 ASR，随后只覆盖中文列。
            return {"kind": "zh_overlay", "count": 1}
        plain_timing = (
            "qwen"
            if not bool(item.get("transcript_timed"))
            and language in {"ja", "en"}
            and _qwen_script_alignment_available(paths)
            else "estimate"
        )
        try:
            from asmr_dubber.models import load_project
            from asmr_dubber.pipeline import import_project_transcript

            project, project_dir = load_project(project_json)
            result = import_project_transcript(
                project,
                project_dir,
                transcript_path=transcript,
                plain_timing=plain_timing,
                script_language=language,
                progress=lambda message, current, total: print(f"  {message}"),
            )
        except Exception as exc:
            raise VideoPreparerError(f"自动导入台本/字幕失败：{exc}") from exc
        return {
            "kind": "direct",
            "language": language,
            "path": str(transcript),
            **result,
        }

    # 合并模式只有在每一轨都有同语言的时间轴字幕时才直接替代 ASR。
    if len(usable) != len(timeline):
        if any(
            str(item.get("transcript_language") or "") == "zh"
            and bool(item.get("transcript_timed"))
            for item in usable
        ):
            return {"kind": "zh_overlay_partial", "count": len(usable)}
        return {"kind": "partial", "count": len(usable)}
    languages = {str(item["transcript_language"]) for item in usable}
    if len(languages) != 1 or not all(bool(item.get("transcript_timed")) for item in usable):
        if any(
            str(item.get("transcript_language") or "") == "zh"
            and bool(item.get("transcript_timed"))
            for item in usable
        ):
            return {"kind": "zh_overlay_partial", "count": len(usable)}
        return {"kind": "partial", "count": len(usable)}
    language = languages.pop()
    if language == "zh":
        return {"kind": "zh_overlay", "count": len(usable)}

    try:
        from asmr_dubber.models import load_project, save_project
        from asmr_dubber.pipeline import export_transcript

        project, project_dir = load_project(project_json)
        combined = []
        for item in timeline:
            duration = int(item["duration_samples"]) / SAMPLE_RATE
            parsed = _load_transcript_sentences(
                Path(str(item["transcript"])),
                language=language,
                duration_seconds=duration,
            )
            offset = int(item["start_samples"]) / SAMPLE_RATE
            for sentence in parsed.sentences:
                copied = sentence.model_copy(deep=True)
                copied.id = f"s{len(combined) + 1:06d}"
                copied.start_seconds += offset
                copied.end_seconds += offset
                combined.append(copied)
        project.sentences = combined
        project.source_language = language
        project.asr_language = "自动导入的分轨时间轴字幕"
        project.settings.tts_reference_sentence_id = None
        save_project(project, project_dir)
        export_transcript(project, project_dir)
    except Exception as exc:
        raise VideoPreparerError(f"无法合并分轨字幕：{exc}") from exc
    return {"kind": "direct", "language": language, "sentences": len(combined)}


def overlay_timed_chinese_transcripts(
    project_json: Path,
    timeline: list[dict[str, Any]],
) -> int:
    chinese_cues: list[Any] = []
    for item in timeline:
        if (
            str(item.get("transcript_language") or "") != "zh"
            or not item.get("transcript")
            or not bool(item.get("transcript_timed"))
        ):
            continue
        duration = int(item["duration_samples"]) / SAMPLE_RATE
        parsed = _load_transcript_sentences(
            Path(str(item["transcript"])),
            language="zh",
            duration_seconds=duration,
        )
        offset = int(item["start_samples"]) / SAMPLE_RATE
        for cue in parsed.sentences:
            copied = cue.model_copy(deep=True)
            copied.start_seconds += offset
            copied.end_seconds += offset
            chinese_cues.append(copied)
    if not chinese_cues:
        return 0

    try:
        from asmr_dubber.models import load_project, save_project
        from asmr_dubber.pipeline import export_transcript

        project, project_dir = load_project(project_json)
        assignments: dict[int, list[str]] = {}
        for cue in chinese_cues:
            best_index = -1
            best_overlap = 0.0
            cue_midpoint = (cue.start_seconds + cue.end_seconds) / 2
            best_distance = float("inf")
            for index, sentence in enumerate(project.sentences):
                overlap = max(
                    0.0,
                    min(cue.end_seconds, sentence.end_seconds)
                    - max(cue.start_seconds, sentence.start_seconds),
                )
                distance = abs(
                    cue_midpoint - (sentence.start_seconds + sentence.end_seconds) / 2
                )
                if overlap > best_overlap or (
                    overlap == best_overlap and overlap > 0 and distance < best_distance
                ):
                    best_index = index
                    best_overlap = overlap
                    best_distance = distance
            if best_index < 0:
                nearest = min(
                    range(len(project.sentences)),
                    key=lambda index: abs(
                        cue_midpoint
                        - (
                            project.sentences[index].start_seconds
                            + project.sentences[index].end_seconds
                        )
                        / 2
                    ),
                    default=-1,
                )
                if nearest >= 0:
                    distance = abs(
                        cue_midpoint
                        - (
                            project.sentences[nearest].start_seconds
                            + project.sentences[nearest].end_seconds
                        )
                        / 2
                    )
                    if distance <= 3.0:
                        best_index = nearest
            if best_index >= 0:
                assignments.setdefault(best_index, []).append(cue.zh_text)

        applied = 0
        for index, texts in assignments.items():
            text = " ".join(part.strip() for part in texts if part.strip()).strip()
            if text:
                project.sentences[index].zh_text = text
                project.sentences[index].status = "translated"
                applied += 1
        if applied:
            save_project(project, project_dir)
            export_transcript(project, project_dir)
        return applied
    except Exception as exc:
        raise VideoPreparerError(f"无法合并已有中文字幕：{exc}") from exc


def shift_srt_text(text: str, offset_ms: int) -> str:
    timestamp = re.compile(r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})")

    def replace(match: re.Match[str]) -> str:
        hours, minutes, seconds, millis = map(int, match.groups())
        total = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis + offset_ms
        total = max(0, total)
        out_hours, remainder = divmod(total, 3_600_000)
        out_minutes, remainder = divmod(remainder, 60_000)
        out_seconds, out_millis = divmod(remainder, 1000)
        return f"{out_hours:02d}:{out_minutes:02d}:{out_seconds:02d},{out_millis:03d}"

    lines = []
    for line in text.splitlines():
        lines.append(timestamp.sub(replace, line) if "-->" in line else line)
    return "\n".join(lines) + "\n"


def shift_lrc_text(text: str, offset_ms: int) -> str:
    timestamp = re.compile(r"\[(\d+):(\d{2})(?:[.](\d{2,3}))?\]")

    def replace(match: re.Match[str]) -> str:
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        fraction_text = match.group(3) or "00"
        fraction_ms = int(fraction_text.ljust(3, "0")[:3])
        total = (minutes * 60 + seconds) * 1000 + fraction_ms + offset_ms
        total = max(0, total)
        out_minutes, remainder = divmod(total, 60_000)
        out_seconds, out_millis = divmod(remainder, 1000)
        return f"[{out_minutes:02d}:{out_seconds:02d}.{out_millis // 10:02d}]"

    return timestamp.sub(replace, text).rstrip("\n") + "\n"


def atomic_write_text(destination: Path, text: str, *, encoding: str = "utf-8-sig") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding=encoding)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_subtitles(
    project_json: Path,
    folder: Path,
    mode: str,
    *,
    harmonized_delay_seconds: int,
) -> tuple[Path, Path]:
    project = read_project(project_json)
    srt_source = project_asset(project_json, project.get("subtitle_srt_file"))
    lrc_source = project_asset(project_json, project.get("subtitle_lrc_file"))
    if srt_source is None or lrc_source is None:
        raise VideoPreparerError("ASMR Dubber 没有生成完整的 SRT/LRC 字幕。")

    srt_destination = folder / "双语版.srt"
    lrc_destination = folder / "双语版.lrc"
    if normalize_mode(mode) == MODE_VIDEO_HARMONIZED:
        offset_ms = harmonized_delay_seconds * 1000
        srt_text = srt_source.read_text(encoding="utf-8-sig")
        lrc_text = lrc_source.read_text(encoding="utf-8-sig")
        atomic_write_text(srt_destination, shift_srt_text(srt_text, offset_ms))
        atomic_write_text(lrc_destination, shift_lrc_text(lrc_text, offset_ms))
    else:
        atomic_copy(srt_source, srt_destination)
        atomic_copy(lrc_source, lrc_destination)
    return srt_destination, lrc_destination


def remux_video_with_subtitle(
    paths: ToolPaths,
    source: Path,
    subtitle_file: Path,
    destination: Path,
) -> None:
    """Convert an MKV fallback to MP4 while keeping a selectable subtitle track."""
    attempts = (
        ["-c:v", "copy"],
        [*paths.video_encoder_options, "-pix_fmt", "yuv420p"],
    )
    failures: list[str] = []
    for video_options in attempts:
        partial = partial_output_path(destination)
        try:
            run_ffmpeg(
                paths,
                [
                    "-loglevel",
                    "warning",
                    "-i",
                    str(source),
                    "-f",
                    "srt",
                    "-i",
                    str(subtitle_file),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-map",
                    "1:s:0",
                    *video_options,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "256k",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-movflags",
                    "+faststart",
                    str(partial),
                ],
            )
            os.replace(partial, destination)
            return
        except VideoPreparerError as exc:
            failures.append(str(exc))
        finally:
            partial.unlink(missing_ok=True)
    raise VideoPreparerError("无法把 ASMR Dubber 的字幕视频转换成 MP4：" + "；".join(failures))


def copy_final_outputs(
    paths: ToolPaths,
    project_json: Path,
    folder: Path,
    mode: str,
    *,
    harmonized_delay_seconds: int,
    harmonized_volume_db: float,
) -> dict[str, str]:
    project = read_project(project_json)
    mode = normalize_mode(mode)
    if mode == MODE_AUDIO:
        audio_source = project_asset(project_json, project.get("output_file"))
        if audio_source is None:
            raise VideoPreparerError("ASMR Dubber 没有生成可用的双语音频。")
        srt, lrc = copy_subtitles(
            project_json,
            folder,
            mode,
            harmonized_delay_seconds=harmonized_delay_seconds,
        )
        audio_suffix = audio_source.suffix if audio_source.suffix else ".wav"
        audio_destination = folder / f"双语版{audio_suffix}"
        print("  正在把双语音频送回原文件夹……")
        atomic_copy(audio_source, audio_destination)
        return {
            "audio": str(audio_destination),
            "srt": str(srt),
            "lrc": str(lrc),
        }

    mixed_audio_source: Path | None = None
    if mode == MODE_VIDEO_HARMONIZED:
        # Do not freeze the first frame of a hard-subtitled video for 20 minutes.
        # Delay the clean mixed video, then attach the already shifted subtitle.
        video_source = project_asset(project_json, project.get("output_video_file"))
        if video_source is None:
            video_source = project_asset(project_json, project.get("subtitle_video_file"))
        # Use ASMR Dubber's lossless mixed WAV as the final audio source. The
        # delayed harmonious version is then encoded to AAC only once.
        mixed_audio_source = project_asset(project_json, project.get("output_file"))
    else:
        video_source = project_asset(project_json, project.get("subtitle_video_file"))
        if video_source is None:
            video_source = project_asset(project_json, project.get("output_video_file"))
    if video_source is None:
        raise VideoPreparerError("ASMR Dubber 没有生成可用的双语视频。")

    srt, lrc = copy_subtitles(
        project_json,
        folder,
        mode,
        harmonized_delay_seconds=harmonized_delay_seconds,
    )
    video_destination = folder / "双语版.mp4"
    print("  正在把双语视频送回原文件夹……")
    if mode == MODE_VIDEO_HARMONIZED:
        render_delayed_existing_video(
            paths,
            video_source,
            video_destination,
            lead_seconds=harmonized_delay_seconds,
            subtitle_file=srt,
            volume_db=harmonized_volume_db,
            audio_source=mixed_audio_source,
        )
    else:
        if video_source.suffix.casefold() == ".mp4":
            atomic_copy(video_source, video_destination)
        else:
            remux_video_with_subtitle(paths, video_source, srt, video_destination)
    return {
        "video": str(video_destination),
        "srt": str(srt),
        "lrc": str(lrc),
    }


def concat_audio_files(
    paths: ToolPaths,
    files: Sequence[Path],
    workspace: Path,
    *,
    expected_samples: Sequence[int] | None = None,
    name: str = "combined",
) -> tuple[Path, list[int]]:
    """Normalize and concatenate already-produced audio files.

    ASMR Dubber normally preserves the source duration, but a backend can
    differ by a few samples.  When the source timeline is available we pad or
    trim each part to that exact length before concatenation, so the resulting
    subtitle offsets remain deterministic.
    """

    if not files:
        raise VideoPreparerError("没有可合并的音频文件。")
    if expected_samples is not None and len(expected_samples) != len(files):
        raise VideoPreparerError("合并音频的时长清单与文件数量不一致。")
    workspace.mkdir(parents=True, exist_ok=True)
    segments = workspace / f"{name}-segments"
    segments.mkdir(parents=True, exist_ok=True)
    actual: list[int] = []
    for index, source in enumerate(files, start=1):
        source = source.resolve()
        if not source.is_file():
            raise VideoPreparerError(f"找不到要合并的音频：{source}")
        destination = segments / f"seg_{index:06d}.flac"
        filters = [
            f"aresample={SAMPLE_RATE}:async=0",
            f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo",
        ]
        target = int(expected_samples[index - 1]) if expected_samples is not None else 0
        if target > 0:
            filters.extend((f"apad=whole_len={target}", f"atrim=end_sample={target}"))
        filters.append("asetpts=N/SR/TB")
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "warning",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                ",".join(filters),
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "2",
                "-sample_fmt",
                "s16",
                "-c:a",
                "flac",
                "-compression_level",
                "0",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                str(destination),
            ],
        )
        measured = audio_duration_samples(paths, destination)
        if target > 0 and abs(measured - target) > 1:
            raise VideoPreparerError(
                f"无法把第 {index} 段音频规整到原始时长（{measured}/{target} 采样）。"
            )
        actual.append(measured)

    concat_file = workspace / f"{name}.ffconcat"
    concat_file.write_text(
        "ffconcat version 1.0\n"
        + "\n".join(
            f"file '{segments.name}/seg_{index:06d}.flac'"
            for index in range(1, len(files) + 1)
        )
        + "\n",
        encoding="ascii",
    )
    destination = workspace / f"{name}.flac"
    run_ffmpeg(
        paths,
        [
            "-loglevel",
            "warning",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            concat_file.name,
            "-map",
            "0:a:0",
            "-af",
            "asetpts=N/SR/TB",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "flac",
            "-compression_level",
            "0",
            str(destination),
        ],
        cwd=workspace,
    )
    measured_total = audio_duration_samples(paths, destination)
    expected_total = sum(actual)
    if abs(measured_total - expected_total) > 1:
        raise VideoPreparerError(
            f"合并音频采样数异常：得到 {measured_total}，应为 {expected_total}。"
        )
    return destination, actual


def _srt_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    for raw in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line.rstrip("\r") for line in raw.splitlines()]
        if any("-->" in line for line in lines):
            blocks.append(lines)
    return blocks


def _srt_timestamp_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if match is None:
        raise ValueError(value)
    hours, minutes, seconds, millis = map(int, match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _format_srt_timestamp(milliseconds: int) -> str:
    total = max(0, int(milliseconds))
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def combine_srt_files(
    entries: Sequence[tuple[Path, int] | tuple[Path, int, int]],
    destination: Path,
    *,
    final_offset_ms: int = 0,
) -> Path:
    """Merge SRT files, shifting and re-numbering cues in source order."""

    output: list[str] = []
    sequence = 1
    timing_pattern = re.compile(
        r"^(\d{2,}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
        r"(\d{2,}:\d{2}:\d{2}[,.]\d{3})(.*)$"
    )
    for entry in entries:
        source, offset_ms = entry[0], entry[1]
        clip_end_ms = entry[2] if len(entry) == 3 else None
        if not source.is_file():
            raise VideoPreparerError(f"找不到字幕文件：{source}")
        text = source.read_text(encoding="utf-8-sig")
        for block in _srt_blocks(text):
            timing_index = next(
                (index for index, line in enumerate(block) if "-->" in line),
                None,
            )
            if timing_index is None:
                continue
            shifted = shift_srt_text(
                block[timing_index] + "\n",
                int(offset_ms) + int(final_offset_ms),
            ).strip("\r\n")
            timing_match = timing_pattern.match(shifted)
            if timing_match is not None and clip_end_ms is not None:
                cue_start = _srt_timestamp_ms(timing_match.group(1))
                cue_end = _srt_timestamp_ms(timing_match.group(2))
                absolute_limit = int(clip_end_ms) + int(final_offset_ms)
                if cue_start >= absolute_limit:
                    continue
                cue_end = min(cue_end, absolute_limit)
                if cue_end <= cue_start:
                    continue
                shifted = (
                    f"{_format_srt_timestamp(cue_start)} --> "
                    f"{_format_srt_timestamp(cue_end)}{timing_match.group(3)}"
                )
            payload = [str(sequence), shifted, *block[timing_index + 1 :]]
            output.extend(payload)
            output.append("")
            sequence += 1
    if not output:
        raise VideoPreparerError("字幕文件中没有可合并的 SRT 时间轴。")
    atomic_write_text(destination, "\n".join(output).rstrip() + "\n")
    return destination


def combine_lrc_files(
    entries: Sequence[tuple[Path, int]],
    destination: Path,
    *,
    final_offset_ms: int = 0,
) -> Path:
    """Merge LRC files while retaining metadata from the first file only."""

    timestamp_line = re.compile(r"\[\d+:\d{2}(?:[.]\d{1,3})?\]")
    headers: list[str] = []
    timed_lines: list[str] = []
    for entry_index, (source, offset_ms) in enumerate(entries):
        if not source.is_file():
            raise VideoPreparerError(f"找不到字幕文件：{source}")
        text = source.read_text(encoding="utf-8-sig")
        shifted = shift_lrc_text(text, int(offset_ms) + int(final_offset_ms))
        for line in shifted.splitlines():
            if timestamp_line.search(line):
                timed_lines.append(line)
            elif entry_index == 0 and line.strip():
                headers.append(line)
    if not timed_lines:
        raise VideoPreparerError("字幕文件中没有可合并的 LRC 时间轴。")
    atomic_write_text(destination, "\n".join((*headers, *timed_lines)) + "\n")
    return destination


def render_static_bilingual_video(
    paths: ToolPaths,
    audio_source: Path,
    background: Path | None,
    subtitle_file: Path,
    destination: Path,
    *,
    lead_seconds: int = 0,
    volume_db: float = 0.0,
) -> None:
    """Render a lightweight static video, preferring visible hard subtitles."""

    clean_video = destination.with_name(f".{destination.stem}.clean-{uuid.uuid4().hex}.mp4")
    render_dir: Path | None = None
    try:
        render_static_video(
            paths,
            audio_source,
            background,
            clean_video,
            lead_seconds=lead_seconds,
            volume_db=volume_db,
        )
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        render_dir = Path(tempfile.mkdtemp(prefix="subtitle-render-", dir=WORK_ROOT))
        local_subtitle = render_dir / "subtitle.srt"
        shutil.copy2(subtitle_file, local_subtitle)
        partial = partial_output_path(destination)
        try:
            run_ffmpeg(
                paths,
                [
                    "-loglevel",
                    "warning",
                    "-i",
                    str(clean_video),
                    "-vf",
                    "subtitles=filename=subtitle.srt:charenc=UTF-8",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    *paths.video_encoder_options,
                    "-r",
                    str(VIDEO_FPS),
                    "-fps_mode",
                    "cfr",
                    "-c:a",
                    "copy",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(partial),
                ],
                cwd=render_dir,
            )
            os.replace(partial, destination)
        except VideoPreparerError:
            partial.unlink(missing_ok=True)
            print("  当前 FFmpeg 无法烧录字幕，改为写入可选择的内嵌字幕轨。")
            remux_video_with_subtitle(paths, clean_video, subtitle_file, destination)
        finally:
            partial.unlink(missing_ok=True)
    finally:
        clean_video.unlink(missing_ok=True)
        if render_dir is not None:
            shutil.rmtree(render_dir, ignore_errors=True)


def translate_titles(
    state: dict[str, Any],
    paths: ToolPaths,
) -> dict[str, str]:
    try:
        from asmr_dubber.models import Sentence
        from asmr_dubber.translation import translate_sentences
        from asmr_dubber.user_settings import (
            PROVIDER_PRESETS,
            load_user_settings,
            resolve_api_key,
        )
    except Exception as exc:
        raise VideoPreparerError(f"无法加载 ASMR Dubber 的翻译工具：{exc}") from exc

    try:
        settings = load_user_settings()
        provider = settings.translation_provider
        preset = PROVIDER_PRESETS.get(provider)
        if preset is None:
            raise VideoPreparerError(f"ASMR Dubber 中选择了未知翻译服务：{provider}")
        model = settings.translation_model or str(preset.get("default_model") or "")
        base_url = settings.translation_base_url.strip() or str(preset.get("base_url") or "")
        temperature = settings.translation_temperature
        top_p = settings.translation_top_p
        max_tokens = settings.translation_max_output_tokens
        api_key = resolve_api_key(provider)
    except Exception as exc:
        raise VideoPreparerError(f"无法读取 ASMR Dubber 的翻译设置或密钥：{exc}") from exc

    cached = {
        str(key): str(value)
        for key, value in (state.get("title_translations") or {}).items()
        if str(value).strip()
    }
    folder_name_original = str(
        state.get("folder_name_original") or Path(state["source_folder"]).name
    ).strip()
    folder_sentence = Sentence(
        id="folder0000",
        start_seconds=0.0,
        end_seconds=1.0,
        ja_text=folder_name_original,
        zh_text=str(state.get("folder_name_translation") or "").strip(),
    )
    sentences = [folder_sentence]
    for index, item in enumerate(state["timeline"], start=1):
        filename = str(item.get("relative_path") or item["filename"])
        sentences.append(
            Sentence(
                id=f"title{index:04d}",
                start_seconds=float(index),
                end_seconds=float(index + 1),
                ja_text=str(item["title_ja"]),
                zh_text=cached.get(filename, ""),
            )
        )
    source_languages = {
        str(item.get("source_language") or "ja").casefold()
        for item in state["timeline"]
    }
    source_language = "en" if source_languages == {"en"} else "ja"

    try:
        translate_sentences(
            sentences,
            api_key=api_key,
            provider=provider,
            source_language=source_language,
            model=model,
            base_url=base_url,
            system_prompt=TITLE_TRANSLATION_PROMPT,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_tokens,
            deepl_formality=settings.translation_deepl_formality,
            microsoft_region=settings.translation_microsoft_region,
            send_context=True,
            context_sentences=100,
            memory_sentences=50,
            job_id=f"video_preparer_{task_key(Path(state['source_folder']))}",
            progress=lambda message, current, total: print(f"  {message}"),
        )
    except Exception as exc:
        if folder_sentence.zh_text.strip():
            state["folder_name_translation"] = folder_sentence.zh_text.strip()
        for item, sentence in zip(state["timeline"], sentences[1:], strict=True):
            if sentence.zh_text.strip():
                key = str(item.get("relative_path") or item["filename"])
                cached[key] = sentence.zh_text.strip()
        state["title_translations"] = cached
        raise VideoPreparerError(f"文件夹名称与短音频标题翻译失败：{exc}") from exc

    translated: dict[str, str] = {}
    missing: list[str] = []
    folder_name_translation = folder_sentence.zh_text.strip()
    if not folder_name_translation:
        missing.append("文件夹名称")
    else:
        state["folder_name_original"] = folder_name_original
        state["folder_name_translation"] = folder_name_translation
    for item, sentence in zip(state["timeline"], sentences[1:], strict=True):
        title = sentence.zh_text.strip()
        key = str(item.get("relative_path") or item["filename"])
        if not title:
            missing.append(key)
            continue
        translated[key] = title
    state["title_translations"] = translated
    if missing:
        raise VideoPreparerError("以下名称或标题翻译为空：" + "、".join(missing))
    return translated


def translated_plan_titles(
    plan_id: str,
    source_folder: Path,
    sources: list[AudioSource],
    paths: ToolPaths,
) -> tuple[str, dict[str, str]]:
    cached = load_plan_metadata(plan_id)
    expected_keys = [item.relative_path or item.path.name for item in sources]
    translations = {
        str(key): str(value)
        for key, value in (cached.get("title_translations") or {}).items()
        if str(value).strip()
    }
    folder_translation = str(cached.get("folder_name_translation") or "").strip()
    if folder_translation and all(str(translations.get(key) or "").strip() for key in expected_keys):
        return folder_translation, translations

    state: dict[str, Any] = {
        "source_folder": str(source_folder),
        "folder_name_original": source_folder.name,
        "folder_name_translation": folder_translation,
        "title_translations": translations,
        "timeline": [
            {
                "filename": item.path.name,
                "relative_path": item.relative_path or item.path.name,
                "title_ja": item.title_ja,
                "source_language": item.source_language,
            }
            for item in sources
        ],
    }
    try:
        translations = translate_titles(state, paths)
    except VideoPreparerError:
        save_plan_metadata(
            plan_id,
            {
                "source_folder": str(source_folder),
                "folder_name_original": source_folder.name,
                "folder_name_translation": str(state.get("folder_name_translation") or ""),
                "title_translations": dict(state.get("title_translations") or {}),
            },
        )
        raise
    folder_translation = str(state["folder_name_translation"])
    save_plan_metadata(
        plan_id,
        {
            "source_folder": str(source_folder),
            "folder_name_original": source_folder.name,
            "folder_name_translation": folder_translation,
            "title_translations": translations,
        },
    )
    return folder_translation, translations


def bracketed(value: str) -> str:
    text = value.strip()
    if text.startswith("【") and text.endswith("】"):
        return text
    return f"【{text}】"


def format_timestamp_from_samples(samples: int) -> str:
    # 向下取整，保证显示时间不会晚于音频真正开始的时刻。
    seconds = max(0, samples // SAMPLE_RATE)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def write_timestamp_document(state: dict[str, Any], folder: Path) -> Path:
    translations = state.get("title_translations") or {}
    folder_name_original = str(
        state.get("folder_name_original") or Path(state["source_folder"]).name
    ).strip()
    folder_name_translation = str(state.get("folder_name_translation") or "").strip()
    if not folder_name_translation:
        raise VideoPreparerError("缺少文件夹名称的中文翻译。")
    offset_samples = (
        int(state.get("harmonized_delay_seconds") or 0) * SAMPLE_RATE
        if normalize_mode(state["mode"]) == MODE_VIDEO_HARMONIZED
        else 0
    )
    lines: list[str] = [
        f"中文名称：{folder_name_translation}",
        f"原始名称：{folder_name_original}",
        "",
    ]
    for item in state["timeline"]:
        filename = str(item.get("relative_path") or item["filename"])
        chinese = str(translations.get(filename, "")).strip()
        if not chinese:
            raise VideoPreparerError(f"缺少中文标题：{filename}")
        start = int(item["start_samples"]) + offset_samples
        lines.append(f"{format_timestamp_from_samples(start)} {bracketed(chinese)}")
        lines.append(bracketed(str(item["title_ja"])))
        lines.append("")
    stored_footer = state.get("timestamp_footer")
    footer = (
        DEFAULT_TIMESTAMP_FOOTER
        if stored_footer is None
        else str(stored_footer).strip()
    )
    if footer:
        lines.append(footer)
    destination = folder / "时间戳.txt"
    atomic_write_text(destination, "\n".join(lines).rstrip() + "\n")
    return destination


def ask_mode(config: AppConfig) -> str:
    while True:
        print("\n请选择处理类型：")
        print("  1. 纯音频模式（不制作视频，输出音频与字幕）")
        print("  2. 静态视频模式（下一步选择普通或和谐）")
        answer = input("输入 1 或 2：").strip()
        if answer == "1":
            return MODE_AUDIO
        if answer == "2":
            while True:
                print("\n请选择视频分支：")
                print("  1. 普通模式（静态背景 + 原音量）")
                print(
                    f"  2. 和谐模式（成品音量 {config.harmonized_volume_db:g} dB，"
                    f"视频与字幕延后 {config.harmonized_delay_seconds / 60:g} 分钟）"
                )
                video_answer = input("输入 1 或 2；输入 B 返回：").strip().casefold()
                if video_answer == "1":
                    return MODE_VIDEO_NORMAL
                if video_answer == "2":
                    return MODE_VIDEO_HARMONIZED
                if video_answer == "b":
                    break
                print("输入无效，请重新选择。")
            continue
        print("输入无效，请重新选择。")


def expected_output_paths(folder: Path, mode: str) -> tuple[Path, ...]:
    mode = normalize_mode(mode)
    primary = (
        (folder / "原声.flac", folder / "双语版.wav", folder / "双语版.flac")
        if mode == MODE_AUDIO
        else (folder / "原声.mp4", folder / "双语版.mp4")
    )
    return (
        *primary,
        folder / "双语版.srt",
        folder / "双语版.lrc",
        folder / "时间戳.txt",
    )


def confirm_overwrite(folder: Path, mode: str, *, force: bool) -> None:
    existing = [
        path
        for path in expected_output_paths(folder, mode)
        if path.exists()
    ]
    if not existing or force:
        return
    print("\n以下旧文件将在相应新文件完整生成后被替换：")
    for path in existing:
        print(f"  {path.name}")
    answer = input("继续吗？输入 Y 确认：").strip().casefold()
    if answer != "y":
        raise KeyboardInterrupt


def create_initial_state(
    folder: Path,
    mode: str,
    sources: list[AudioSource],
    background: Path | None,
    config: AppConfig,
) -> dict[str, Any]:
    mode = normalize_mode(mode)
    return {
        "schema": 1,
        "source_folder": str(folder),
        "mode": mode,
        "harmonized_volume_db": config.harmonized_volume_db,
        "harmonized_delay_seconds": config.harmonized_delay_seconds,
        "timestamp_footer": config.timestamp_footer,
        "status": "",
        "fingerprint": fingerprint(sources, background),
        "background": str(background) if background else None,
        "workspace": str(workspace_path(folder)),
        "timeline": [],
        "title_translations": {},
        "folder_name_original": folder.name,
        "folder_name_translation": "",
        "timestamp_schema": 0,
        "outputs": {},
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def create_planned_state(
    source_folder: Path,
    output_folder: Path,
    mode: str,
    sources: list[AudioSource],
    background: Path | None,
    config: AppConfig,
    *,
    plan_id: str,
    job_id: str,
    folder_translation: str,
    title_translations: dict[str, str],
) -> dict[str, Any]:
    state = create_initial_state(
        output_folder,
        mode,
        sources,
        background,
        config,
    )
    state.update(
        {
            "source_folder": str(source_folder),
            "output_folder": str(output_folder),
            "workspace": str(planned_workspace_path(source_folder, plan_id, job_id)),
            "plan_id": plan_id,
            "job_id": job_id,
            "folder_name_original": source_folder.name,
            "folder_name_translation": folder_translation,
            "title_translations": {
                str(key): str(value) for key, value in title_translations.items()
            },
        }
    )
    return state


def execute_planned_job(
    paths: ToolPaths,
    config: AppConfig,
    *,
    source_folder: Path,
    output_folder: Path,
    mode: str,
    sources: list[AudioSource],
    background: Path | None,
    plan_id: str,
    job_id: str,
    folder_translation: str,
    title_translations: dict[str, str],
    rebuild: bool,
    shared_reference: dict[str, str] | None = None,
    capture_reference_path: Path | None = None,
) -> dict[str, Any]:
    output_folder.mkdir(parents=True, exist_ok=True)
    state_file = planned_state_path(source_folder, plan_id, job_id)
    state = None if rebuild else load_state(state_file)
    current_fingerprint = fingerprint(sources, background)

    if state is not None and not rebuild:
        if state.get("fingerprint") != current_fingerprint:
            raise VideoPreparerError(
                f"{output_folder.name} 的源音频、字幕或背景发生变化；请使用 --rebuild 重做。"
            )
        missing = missing_resume_artifacts(state, output_folder)
        if missing:
            timestamp_file = output_folder / "时间戳.txt"
            if missing == [timestamp_file] and status_at_least(state, "outputs_ready"):
                print(f"\n{output_folder.name} 只缺时间戳文档，正在直接补写。")
                state["status"] = "outputs_ready"
                save_state(state_file, state)
            else:
                print(f"\n{output_folder.name} 的旧任务缺少文件，将从头重做。")
                rebuild = True
        elif status_at_least(state, "completed"):
            print(f"\n跳过已经完成的任务：{output_folder.name}")
            return state
        else:
            print(
                f"\n继续未完成任务：{output_folder.name} "
                f"（阶段：{state.get('status') or '尚未开始'}）"
            )

    if state is None or rebuild:
        workspace = planned_workspace_path(source_folder, plan_id, job_id)
        safe_reset_workspace(workspace)
        state = create_planned_state(
            source_folder,
            output_folder,
            mode,
            sources,
            background,
            config,
            plan_id=plan_id,
            job_id=job_id,
            folder_translation=folder_translation,
            title_translations=title_translations,
        )
        save_state(state_file, state)

    execute_task(
        paths,
        output_folder,
        state_file,
        state,
        sources,
        shared_reference=shared_reference,
        capture_reference_path=capture_reference_path,
    )
    return load_state(state_file) or state


def _single_track_source(source: AudioSource) -> AudioSource:
    return replace(source, order=1)


def _smart_track_folder_name(index: int, source: AudioSource) -> str:
    title = safe_filename_component(source.title_ja, fallback=f"音轨 {index}", limit=56)
    return f"{index:03d} {title}"


def clear_smart_outputs(output_root: Path) -> None:
    """Remove only files AutoFlow owns inside its output directory."""

    output_root = output_root.resolve()
    if output_root == output_root.parent:
        raise VideoPreparerError("拒绝清理文件系统根目录。")
    for name in ("合并版", "分轨", ".autoflow"):
        target = output_root / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for name in ("处理清单.json", "曲目清单.txt", "总时间戳.txt"):
        (output_root / name).unlink(missing_ok=True)


def cleanup_completed_workspace(state: dict[str, Any]) -> None:
    workspace = Path(str(state.get("workspace") or "")).expanduser()
    if not workspace.exists():
        return
    root = WORK_ROOT.resolve()
    target = workspace.resolve()
    if target.parent == root and status_at_least(state, "completed"):
        shutil.rmtree(target, ignore_errors=True)


def prepare_smart_output_root(
    output_root: Path,
    *,
    plan_id: str,
    force: bool,
    rebuild: bool,
) -> None:
    if output_root.exists() and not output_root.is_dir():
        raise VideoPreparerError(f"输出路径已经被同名文件占用：{output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = output_root / "处理清单.json"
    previous_plan = ""
    if manifest.is_file():
        try:
            previous_plan = str(
                json.loads(manifest.read_text(encoding="utf-8-sig")).get("plan_id") or ""
            )
        except (OSError, ValueError, json.JSONDecodeError):
            previous_plan = "invalid"
    generated_exists = any(
        (output_root / name).exists()
        for name in ("合并版", "分轨", ".autoflow", "处理清单.json")
    )
    needs_clear = rebuild or (generated_exists and previous_plan != plan_id)
    if not needs_clear:
        return
    if not force and generated_exists:
        print("\nAutoFlow 输出目录中已有另一项任务的文件：")
        print(f"  {output_root}")
        answer = input("清理 AutoFlow 自己生成的旧内容并继续吗？输入 Y 确认：").strip().casefold()
        if answer != "y":
            raise KeyboardInterrupt
    clear_smart_outputs(output_root)


def smart_job_descriptors(
    output_root: Path,
    layout: str,
    sources: Sequence[AudioSource],
) -> list[dict[str, Any]]:
    layout = normalize_layout(layout)
    descriptors: list[dict[str, Any]] = []
    if layout in {LAYOUT_SEPARATE, LAYOUT_BOTH}:
        for index, source in enumerate(sources, start=1):
            name = _smart_track_folder_name(index, source)
            descriptors.append(
                {
                    "job_id": f"track-{index:04d}",
                    "kind": "track",
                    "index": index,
                    "source_indices": [index],
                    "output": str((output_root / "分轨" / name).resolve()),
                    "label": source.title_ja,
                }
            )
    if layout in {LAYOUT_MERGED, LAYOUT_BOTH}:
        descriptors.append(
            {
                "job_id": "merged",
                "kind": "merged",
                "index": 0,
                "source_indices": list(range(1, len(sources) + 1)),
                "output": str((output_root / "合并版").resolve()),
                "label": "合并版",
            }
        )
    return descriptors


def _state_master_audio(
    paths: ToolPaths,
    state: dict[str, Any],
    source: AudioSource,
    workspace: Path,
) -> Path:
    candidate = Path(str(state.get("master_audio") or "")).expanduser()
    if candidate.is_file():
        return candidate
    # A user may have removed .work after a successful run. Recreate only the
    # normalized single-track master needed for a later merge; ASR/TTS remain
    # untouched.
    rebuilt_workspace = workspace / f"rebuild-{source.order:04d}"
    rebuilt_workspace.mkdir(parents=True, exist_ok=True)
    rebuilt, _ = normalize_and_concat(paths, [_single_track_source(source)], rebuilt_workspace)
    return rebuilt


def _state_mixed_audio(
    state: dict[str, Any],
    project_json: Path | None = None,
) -> Path:
    candidate = Path(str(state.get("project_mixed_audio") or "")).expanduser()
    if candidate.is_file():
        return candidate
    if project_json is not None and project_json.is_file():
        project = read_project(project_json)
        candidate = project_asset(project_json, project.get("output_file"))
        if candidate is not None:
            return candidate
    outputs = state.get("outputs") or {}
    candidate = Path(str(outputs.get("audio") or "")).expanduser()
    if candidate.is_file():
        return candidate
    raise VideoPreparerError("ASMR Dubber 已完成，但找不到可合并的双语音频。")


def _timeline_from_states(states: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    offset = 0
    for state in states:
        for item in state.get("timeline") or []:
            copied = dict(item)
            copied["start_samples"] = int(item.get("start_samples") or 0) + offset
            relative = str(item.get("relative_path") or item.get("filename") or "")
            copied["relative_path"] = relative
            merged.append(copied)
        offset += sum(int(item.get("duration_samples") or 0) for item in state.get("timeline") or [])
    return merged


def _subtitle_entries_from_states(
    states: Sequence[dict[str, Any]],
    *,
    mode: str,
) -> tuple[list[tuple[Path, int, int]], list[tuple[Path, int]], int]:
    srt_entries: list[tuple[Path, int, int]] = []
    lrc_entries: list[tuple[Path, int]] = []
    offset_samples = 0
    delay_samples = (
        int(states[0].get("harmonized_delay_seconds") or 0) * SAMPLE_RATE
        if normalize_mode(mode) == MODE_VIDEO_HARMONIZED
        else 0
    )
    for state in states:
        outputs = state.get("outputs") or {}
        srt = Path(str(outputs.get("srt") or "")).expanduser()
        lrc = Path(str(outputs.get("lrc") or "")).expanduser()
        # Per-track harmonious outputs already contain their own delay. Remove
        # it here; the merged product receives exactly one delay at the end.
        per_track_delay = (
            delay_samples
            if normalize_mode(state["mode"]) == MODE_VIDEO_HARMONIZED
            else 0
        )
        shift = offset_samples - per_track_delay
        duration_samples = sum(
            int(item.get("duration_samples") or 0)
            for item in state.get("timeline") or []
        )
        srt_entries.append(
            (
                srt,
                shift * 1000 // SAMPLE_RATE,
                (offset_samples + duration_samples) * 1000 // SAMPLE_RATE,
            )
        )
        lrc_entries.append((lrc, shift * 1000 // SAMPLE_RATE))
        offset_samples += duration_samples
    return srt_entries, lrc_entries, delay_samples * 1000 // SAMPLE_RATE


def build_merged_outputs(
    paths: ToolPaths,
    config: AppConfig,
    *,
    source_folder: Path,
    output_folder: Path,
    mode: str,
    sources: Sequence[AudioSource],
    states: Sequence[dict[str, Any]],
    background: Path | None,
    folder_translation: str,
    title_translations: dict[str, str],
) -> dict[str, str]:
    """Build the merged product from completed per-track projects."""

    mode = normalize_mode(mode)
    output_folder.mkdir(parents=True, exist_ok=True)
    work = WORK_ROOT / f"merge-{task_key(output_folder)}"
    safe_reset_workspace(work)
    expected = [
        sum(int(item.get("duration_samples") or 0) for item in state.get("timeline") or [])
        for state in states
    ]
    masters = [
        _state_master_audio(paths, state, source, work)
        for source, state in zip(sources, states, strict=True)
    ]
    original_master, _ = concat_audio_files(
        paths,
        masters,
        work,
        expected_samples=expected,
        name="original",
    )
    mixed_files = [
        _state_mixed_audio(state, Path(str(state.get("project_json") or "")))
        for state in states
    ]
    mixed_master, _ = concat_audio_files(
        paths,
        mixed_files,
        work,
        expected_samples=expected,
        name="mixed",
    )

    timeline = _timeline_from_states(states)
    merged_state: dict[str, Any] = {
        "source_folder": str(source_folder),
        "mode": mode,
        "harmonized_delay_seconds": int(config.harmonized_delay_seconds),
        "timestamp_footer": config.timestamp_footer,
        "timeline": timeline,
        "title_translations": dict(title_translations),
        "folder_name_original": source_folder.name,
        "folder_name_translation": folder_translation,
    }
    srt_entries, lrc_entries, final_offset_ms = _subtitle_entries_from_states(
        states,
        mode=mode,
    )
    srt = combine_srt_files(
        srt_entries,
        output_folder / "双语版.srt",
        final_offset_ms=final_offset_ms,
    )
    lrc = combine_lrc_files(
        lrc_entries,
        output_folder / "双语版.lrc",
        final_offset_ms=final_offset_ms,
    )

    if mode == MODE_AUDIO:
        original_destination = output_folder / "原声.flac"
        mixed_destination = output_folder / "双语版.flac"
        atomic_copy(original_master, original_destination)
        atomic_copy(mixed_master, mixed_destination)
        outputs = {
            "original": str(original_destination),
            "audio": str(mixed_destination),
            "srt": str(srt),
            "lrc": str(lrc),
        }
    else:
        original_destination = output_folder / "原声.mp4"
        bilingual_destination = output_folder / "双语版.mp4"
        lead = (
            int(config.harmonized_delay_seconds)
            if mode == MODE_VIDEO_HARMONIZED
            else 0
        )
        volume = config.harmonized_volume_db if mode == MODE_VIDEO_HARMONIZED else 0.0
        render_static_video(
            paths,
            original_master,
            background,
            original_destination,
            lead_seconds=lead,
            volume_db=volume,
        )
        render_static_bilingual_video(
            paths,
            mixed_master,
            background,
            srt,
            bilingual_destination,
            lead_seconds=lead,
            volume_db=volume,
        )
        outputs = {
            "original": str(original_destination),
            "video": str(bilingual_destination),
            "srt": str(srt),
            "lrc": str(lrc),
        }
    timestamp = write_timestamp_document(merged_state, output_folder)
    outputs["timestamps"] = str(timestamp)
    shutil.rmtree(work, ignore_errors=True)
    return outputs


def write_smart_summary(
    output_root: Path,
    *,
    source_folder: Path,
    mode: str,
    layout: str,
    sources: Sequence[AudioSource],
    states: Sequence[dict[str, Any]],
    descriptors: Sequence[dict[str, Any]],
    folder_translation: str,
    title_translations: dict[str, str],
    footer: str,
    harmonized_delay_seconds: int,
) -> tuple[Path, Path]:
    """Write a human-readable track index and a timeline reference."""

    output_root.mkdir(parents=True, exist_ok=True)
    descriptor_by_id = {str(item["job_id"]): item for item in descriptors}
    lines = [
        f"中文名称：{folder_translation}",
        f"原始名称：{source_folder.name}",
        f"模式：{mode_label(mode)}",
        f"成品组织：{layout_label(layout)}",
        "",
        "曲目与输出：",
    ]
    timeline_lines = [
        f"中文名称：{folder_translation}",
        f"原始名称：{source_folder.name}",
        "",
    ]
    cumulative = 0
    timeline_offset = (
        harmonized_delay_seconds * SAMPLE_RATE
        if normalize_mode(mode) == MODE_VIDEO_HARMONIZED
        else 0
    )
    for index, (source, state) in enumerate(zip(sources, states, strict=True), start=1):
        key = source.relative_path or source.path.name
        translated = str(title_translations.get(key) or "未翻译").strip()
        descriptor = descriptor_by_id.get(f"track-{index:04d}")
        track_output = ""
        if descriptor is not None:
            try:
                track_output = Path(str(descriptor["output"])).relative_to(output_root).as_posix()
            except ValueError:
                track_output = str(descriptor["output"])
        duration = sum(int(item.get("duration_samples") or 0) for item in state.get("timeline") or [])
        lines.extend(
            (
                f"{index:03d}. {source.title_ja}",
                f"     中文：{translated}",
                f"     原文件：{source.relative_path or source.path.name}",
                f"     输出：{track_output or '仅合并版'}",
                "",
            )
        )
        timeline_lines.append(
            f"{format_timestamp_from_samples(cumulative + timeline_offset)} "
            f"{bracketed(translated)}"
        )
        timeline_lines.extend((bracketed(source.title_ja), ""))
        cumulative += duration

    if layout in {LAYOUT_SEPARATE, LAYOUT_BOTH}:
        lines.extend(
            (
                "说明：分轨文件各自从 00:00 开始；上面的时间轴按原曲目顺序计算，",
                "仅用于查找，不代表把分轨文件直接无缝播放时的播放器时间。",
                "",
            )
        )
    if footer.strip():
        lines.append(footer.strip())
        timeline_lines.append(footer.strip())
    index_file = output_root / "曲目清单.txt"
    timeline_file = output_root / "总时间戳.txt"
    atomic_write_text(index_file, "\n".join(lines).rstrip() + "\n")
    atomic_write_text(timeline_file, "\n".join(timeline_lines).rstrip() + "\n")
    return index_file, timeline_file


def output_mapping_complete(outputs: Any, *, mode: str) -> bool:
    if not isinstance(outputs, dict):
        return False
    required = {"original", "srt", "lrc", "timestamps"}
    required.add("audio" if normalize_mode(mode) == MODE_AUDIO else "video")
    return all(Path(str(outputs.get(key) or "")).is_file() for key in required)


def execute_smart_plan(
    paths: ToolPaths,
    config: AppConfig,
    folder: Path,
    *,
    mode_argument: str | None,
    layout_argument: str | None,
    edition_argument: str | None,
    include_bonus: bool,
    output_root_argument: str | None,
    rebuild: bool,
    force: bool,
) -> None:
    """Run the recursive DLsite catalogue workflow."""

    if not folder.is_dir():
        raise VideoPreparerError(f"文件夹不存在：{folder}")
    output_root = (
        clean_user_path(output_root_argument)
        if output_root_argument
        else (folder / config.output_folder_name).resolve()
    )
    if output_root == folder.resolve():
        raise VideoPreparerError("输出目录不能就是源作品文件夹；请使用 AutoFlow输出 子目录。")

    scan = scan_work(
        folder,
        excluded_directories=(config.output_folder_name, output_root.name),
    )
    edition_label, sources, edition = choose_tracks(
        scan,
        config,
        edition_argument=edition_argument,
        include_bonus=include_bonus,
    )
    if not sources:
        raise VideoPreparerError("选中的版本没有可处理的音轨。")
    mode = normalize_mode(mode_argument) if mode_argument else ask_mode(config)
    layout = ask_output_layout(config, layout_argument)
    background = smart_background(scan, config) if mode != MODE_AUDIO else None
    plan_id = plan_identity(
        folder,
        mode=mode,
        layout=layout,
        edition=edition,
        sources=sources,
        output_root=output_root,
        background=background,
    )
    log_event(
        f"开始智能任务 plan={plan_id} source={folder} mode={mode} layout={layout} "
        f"tracks={len(sources)}"
    )
    prepare_smart_output_root(output_root, plan_id=plan_id, force=force, rebuild=rebuild)

    print(f"\n已选择：{edition_label}")
    print(f"扫描到 {len(sources)} 条音轨；输出目录：{output_root}")
    if mode != MODE_AUDIO:
        if background is not None:
            print(f"背景图片：{background.name}")
        else:
            print("背景图片：黑色背景")

    folder_translation, title_translations = translated_plan_titles(
        plan_id,
        folder,
        list(sources),
        paths,
    )
    descriptors = smart_job_descriptors(output_root, layout, sources)
    manifest = write_plan_manifest(
        output_root / "处理清单.json",
        source_folder=folder,
        output_folder=output_root,
        mode=mode,
        layout=layout,
        edition=edition,
        sources=list(sources),
        background=background,
        plan_id=plan_id,
        jobs=descriptors,
    )
    metadata = load_plan_metadata(plan_id)
    previous_jobs = list(metadata.get("jobs") or [])
    previous_by_id = {
        str(item.get("job_id")): item
        for item in previous_jobs
        if isinstance(item, dict) and item.get("job_id")
    }
    for descriptor in descriptors:
        previous = previous_by_id.get(str(descriptor["job_id"]))
        if previous is None:
            continue
        for key in ("status", "outputs", "project_json"):
            if key in previous:
                descriptor[key] = previous[key]
    metadata.update(
        {
            "source_folder": str(folder),
            "output_folder": str(output_root),
            "mode": mode,
            "layout": layout,
            "edition": edition,
            "background": str(background) if background else None,
            "jobs": descriptors,
        }
    )
    save_plan_metadata(plan_id, metadata)

    states_by_index: dict[int, dict[str, Any]] = {}
    shared_reference = metadata.get("shared_reference")
    if not isinstance(shared_reference, dict) or not Path(
        str(shared_reference.get("audio") or "")
    ).is_file():
        shared_reference = None
    shared_path = output_root / ".autoflow" / "shared-reference.wav"

    if layout == LAYOUT_MERGED:
        descriptor = next(item for item in descriptors if item["kind"] == "merged")
        merged_folder = Path(str(descriptor["output"]))
        state = execute_planned_job(
            paths,
            config,
            source_folder=folder,
            output_folder=merged_folder,
            mode=mode,
            sources=list(sources),
            background=background,
            plan_id=plan_id,
            job_id="merged",
            folder_translation=folder_translation,
            title_translations=title_translations,
            rebuild=rebuild,
        )
        states_by_index = {index: state for index in range(1, len(sources) + 1)}
        states = [state]
    else:
        # Let the longest source establish the shared voice anchor. Processing
        # order is otherwise restored in the original track order for output.
        durations = [audio_duration_samples(paths, item.path) for item in sources]
        anchor_index = max(range(len(sources)), key=lambda index: (durations[index], -index))
        processing_order = [anchor_index, *[index for index in range(len(sources)) if index != anchor_index]]
        print(f"\n分轨模式先处理最长音轨（第 {anchor_index + 1} 条）以建立共用参考。")
        for zero_index in processing_order:
            index = zero_index + 1
            descriptor = next(item for item in descriptors if item.get("index") == index)
            track_source = _single_track_source(sources[zero_index])
            capture = shared_path if shared_reference is None else None
            state = execute_planned_job(
                paths,
                config,
                source_folder=folder,
                output_folder=Path(str(descriptor["output"])),
                mode=mode,
                sources=[track_source],
                background=background,
                plan_id=plan_id,
                job_id=str(descriptor["job_id"]),
                folder_translation=folder_translation,
                title_translations=title_translations,
                rebuild=rebuild,
                shared_reference=shared_reference,
                capture_reference_path=capture,
            )
            states_by_index[index] = state
            if shared_reference is None:
                candidate = state.get("shared_reference")
                if isinstance(candidate, dict) and Path(str(candidate.get("audio") or "")).is_file():
                    shared_reference = candidate
                else:
                    project_json = Path(str(state.get("project_json") or ""))
                    if project_json.is_file() and capture is not None:
                        shared_path.parent.mkdir(parents=True, exist_ok=True)
                        shared_reference = extract_shared_reference(project_json, shared_path)
                        state["shared_reference"] = shared_reference
                        save_state(planned_state_path(folder, plan_id, str(descriptor["job_id"])), state)
                if shared_reference is not None:
                    metadata["shared_reference"] = shared_reference
                    save_plan_metadata(plan_id, metadata)
        states = [states_by_index[index] for index in range(1, len(sources) + 1)]

    if layout == LAYOUT_BOTH:
        merged_descriptor = next(item for item in descriptors if item["kind"] == "merged")
        previous_merged = next(
            (item for item in previous_jobs if item.get("kind") == "merged"),
            {},
        )
        if (
            not rebuild
            and str(previous_merged.get("status") or "") == "completed"
            and output_mapping_complete(previous_merged.get("outputs"), mode=mode)
        ):
            print("\n合并成品已经完整，跳过重复生成。")
            merged_descriptor["outputs"] = dict(previous_merged["outputs"])
            merged_descriptor["status"] = "completed"
        else:
            print("\n分轨项目已完成，正在从分轨结果生成合并成品（不会再次 ASR/TTS）……")
            merged_outputs = build_merged_outputs(
                paths,
                config,
                source_folder=folder,
                output_folder=Path(str(merged_descriptor["output"])),
                mode=mode,
                sources=sources,
                states=states,
                background=background,
                folder_translation=folder_translation,
                title_translations=title_translations,
            )
            merged_descriptor["outputs"] = merged_outputs
            merged_descriptor["status"] = "completed"

    for descriptor in descriptors:
        state: dict[str, Any] | None = None
        if descriptor["kind"] == "track":
            state = states_by_index.get(int(descriptor["index"]))
        elif layout == LAYOUT_MERGED:
            state = states[0]
        if state is not None:
            descriptor["status"] = str(state.get("status") or "")
            descriptor["outputs"] = dict(state.get("outputs") or {})
            descriptor["project_json"] = str(state.get("project_json") or "")

    ordered_states = states if layout != LAYOUT_MERGED else [states[0]]
    if layout == LAYOUT_MERGED:
        # The merged job's timeline already contains the complete order.
        summary_states = []
        for source in sources:
            summary_states.append(
                {
                    "timeline": [
                        {
                            "duration_samples": 0,
                        }
                    ]
                }
            )
        # Use the actual merged timeline to derive per-track durations where
        # possible; this avoids re-opening every source solely for a summary.
        merged_timeline = list(states[0].get("timeline") or [])
        for index, source in enumerate(sources):
            if index < len(merged_timeline):
                summary_states[index] = {
                    "timeline": [
                        {
                            "duration_samples": merged_timeline[index].get("duration_samples", 0)
                        }
                    ]
                }
        summary_states = summary_states
    else:
        summary_states = ordered_states
    write_smart_summary(
        output_root,
        source_folder=folder,
        mode=mode,
        layout=layout,
        sources=sources,
        states=summary_states,
        descriptors=descriptors,
        folder_translation=folder_translation,
        title_translations=title_translations,
        footer=config.timestamp_footer,
        harmonized_delay_seconds=config.harmonized_delay_seconds,
    )
    manifest["jobs"] = descriptors
    manifest["completed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    atomic_write_text(
        output_root / "处理清单.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    metadata["jobs"] = descriptors
    metadata["completed_at"] = manifest["completed_at"]
    save_plan_metadata(plan_id, metadata)
    for state in {str(item.get("job_id")): item for item in states}.values():
        cleanup_completed_workspace(state)
    log_event(f"智能任务完成 plan={plan_id} output={output_root}")
    print("\n智能任务完成。源作品文件没有被修改。")
    print(f"所有成品位于：{output_root}")


def missing_resume_artifacts(state: dict[str, Any], folder: Path) -> list[Path]:
    missing: list[Path] = []

    def require_file(candidate: Path) -> None:
        if not candidate.is_file() and candidate not in missing:
            missing.append(candidate)

    if status_at_least(state, "media_ready"):
        default_original = (
            folder / "原声.flac"
            if normalize_mode(state["mode"]) == MODE_AUDIO
            else folder / "原声.mp4"
        )
        original = Path(str(state.get("original_media") or state.get("original_video") or default_original))
        require_file(original)
    if status_at_least(state, "media_ready") and not status_at_least(
        state, "project_created"
    ):
        dubbing_input = Path(str(state.get("dubbing_input") or ""))
        require_file(dubbing_input)
    if status_at_least(state, "project_created") and not status_at_least(
        state, "outputs_ready"
    ):
        project_json = Path(str(state.get("project_json") or ""))
        require_file(project_json)
    if status_at_least(state, "outputs_ready"):
        outputs = state.get("outputs") or {}
        primary_key = "audio" if normalize_mode(state["mode"]) == MODE_AUDIO else "video"
        default_primary = folder / ("双语版.wav" if primary_key == "audio" else "双语版.mp4")
        require_file(Path(str(outputs.get(primary_key) or default_primary)))
        for key, name in (("srt", "双语版.srt"), ("lrc", "双语版.lrc")):
            require_file(Path(str(outputs.get(key) or folder / name)))
    if status_at_least(state, "completed"):
        timestamp_file = folder / "时间戳.txt"
        require_file(timestamp_file)
    return missing


def prepare_media_phase(
    paths: ToolPaths,
    folder: Path,
    state: dict[str, Any],
    sources: list[AudioSource],
) -> None:
    workspace = Path(state["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    background = Path(state["background"]) if state.get("background") else None
    master, timeline = normalize_and_concat(paths, sources, workspace)
    state["master_audio"] = str(master)
    state["timeline"] = timeline

    mode = normalize_mode(state["mode"])
    if mode == MODE_AUDIO:
        print("\n[2/5] 输出拼接后的无损原声")
        original = folder / "原声.flac"
        atomic_copy(master, original)
        dubbing_input = original
    elif mode == MODE_VIDEO_NORMAL:
        print("\n[2/5] 生成普通静态视频")
        original = folder / "原声.mp4"
        render_static_video(paths, master, background, original)
        dubbing_input = original
    else:
        print("\n[2/5] 生成和谐静态视频")
        original = folder / "原声.mp4"
        dubbing_input = workspace / "dubbing_input.mp4"
        print("  生成供 ASMR Dubber 使用的无前导正常音量版本……")
        render_static_video(paths, master, background, dubbing_input)
        harmonized_volume_db = float(state["harmonized_volume_db"])
        harmonized_delay_seconds = int(state["harmonized_delay_seconds"])
        print(
            f"  生成 {harmonized_volume_db:g} dB 且前置 "
            f"{harmonized_delay_seconds / 60:g} 分钟的原声版本……"
        )
        render_static_video(
            paths,
            master,
            background,
            original,
            lead_seconds=harmonized_delay_seconds,
            volume_db=harmonized_volume_db,
        )
    state["original_media"] = str(original)
    state["original_video"] = str(original) if mode != MODE_AUDIO else None
    state["dubbing_input"] = str(dubbing_input)
    state["status"] = "media_ready"


def execute_task(
    paths: ToolPaths,
    folder: Path,
    state_file: Path,
    state: dict[str, Any],
    sources: list[AudioSource],
    *,
    shared_reference: dict[str, str] | None = None,
    capture_reference_path: Path | None = None,
) -> None:
    if not status_at_least(state, "media_ready"):
        prepare_media_phase(paths, folder, state, sources)
        save_state(state_file, state)

    if not status_at_least(state, "project_created"):
        print("\n[3/5] 创建 ASMR Dubber 项目")
        project_json = create_asmr_project(
            paths,
            Path(state["dubbing_input"]),
            source_language=source_language_for_sources(sources),
        )
        state["project_json"] = str(project_json)
        ensure_autoflow_project_outputs(project_json)
        state["status"] = "project_created"
        save_state(state_file, state)

    project_json = Path(state["project_json"])
    if not project_json.is_file() and not status_at_least(state, "outputs_ready"):
        raise VideoPreparerError(f"ASMR Dubber 项目已经不存在：{project_json}")

    if not status_at_least(state, "analyzed"):
        transcript_result = import_available_source_transcript(
            paths,
            project_json,
            list(state.get("timeline") or []),
        )
        if transcript_result:
            state["transcript_import"] = transcript_result
            kind = str(transcript_result.get("kind") or "")
            if kind == "direct":
                language = str(transcript_result.get("language") or "ja")
                print(
                    f"\n  已自动导入{language.upper()}台本/字幕，"
                    "不再运行不必要的 ASR（语音识别）。"
                )
                state["status"] = "awaiting_reference" if language == "zh" else "analyzed"
                save_state(state_file, state)
            elif kind in {"partial", "zh_overlay_partial"}:
                print(
                    f"\n  找到 {transcript_result.get('count', 0)} 份台本/字幕，"
                    "但不足以覆盖全部合并音轨；本次仍运行 ASR。"
                )

        if not status_at_least(state, "analyzed"):
            print("\n  运行 ASR（语音识别）……")
            run_asmr_cli(paths, "analyze", str(project_json))
            state["status"] = "analyzed"
            save_state(state_file, state)

    transcript_result = state.get("transcript_import") or {}
    if (
        str(transcript_result.get("kind") or "")
        in {"zh_overlay", "zh_overlay_partial"}
        and not state.get("chinese_transcript_overlay_done")
    ):
        applied = overlay_timed_chinese_transcripts(
            project_json,
            list(state.get("timeline") or []),
        )
        state["chinese_transcript_overlay_done"] = True
        state["chinese_transcript_overlay_sentences"] = applied
        print(f"  已把已有中文字幕匹配到 {applied} 条日文识别结果。")
        save_state(state_file, state)

    if not status_at_least(state, "awaiting_reference"):
        if project_source_language(project_json) == "zh":
            print("\n  当前是中文配音稿，跳过 ASR 与翻译。")
        else:
            print("\n  翻译日文……")
            run_asmr_cli(paths, "translate", str(project_json))
        state["status"] = "awaiting_reference"
        save_state(state_file, state)

    if not status_at_least(state, "synthesized"):
        if shared_reference is not None:
            apply_shared_reference(project_json, shared_reference)
            print("\n  已复用本作品的统一音色参考，不需要再次选择。")
        else:
            reference_id = project_reference_id(project_json)
            if reference_id:
                print(f"\n复用已保存的统一音色参考：{reference_id}")
            elif not wait_for_reference(paths, project_json):
                return
        if capture_reference_path is not None and not state.get("shared_reference"):
            state["shared_reference"] = extract_shared_reference(
                project_json,
                capture_reference_path,
            )
            save_state(state_file, state)
        print("\n[5/5] TTS（语音合成）、混音与字幕")
        run_asmr_cli(paths, "synthesize", str(project_json))
        state["status"] = "synthesized"
        save_state(state_file, state)

    if not status_at_least(state, "mixed"):
        run_asmr_cli(paths, "mix", str(project_json))
        project = read_project(project_json)
        mixed_audio = project_asset(project_json, project.get("output_file"))
        if mixed_audio is not None:
            state["project_mixed_audio"] = str(mixed_audio)
        state["status"] = "mixed"
        save_state(state_file, state)

    if not status_at_least(state, "subtitles_ready"):
        run_asmr_cli(paths, "subtitles", str(project_json), "--language", "bilingual")
        state["status"] = "subtitles_ready"
        save_state(state_file, state)

    if not status_at_least(state, "outputs_ready"):
        state["outputs"] = copy_final_outputs(
            paths,
            project_json,
            folder,
            str(state["mode"]),
            harmonized_delay_seconds=int(state["harmonized_delay_seconds"]),
            harmonized_volume_db=float(state["harmonized_volume_db"]),
        )
        state["status"] = "outputs_ready"
        save_state(state_file, state)

    if not status_at_least(state, "completed"):
        translations = state.get("title_translations") or {}
        missing_titles = [
            str(item.get("relative_path") or item["filename"])
            for item in state.get("timeline") or []
            if not str(
                translations.get(str(item.get("relative_path") or item["filename"]), "")
            ).strip()
        ]
        if missing_titles or not str(state.get("folder_name_translation") or "").strip():
            print("\n  使用 ASMR Dubber 当前翻译服务处理作品名和曲目标题……")
            try:
                state["title_translations"] = translate_titles(state, paths)
            except VideoPreparerError:
                save_state(state_file, state)
                raise
        save_state(state_file, state)
        timestamp_file = write_timestamp_document(state, folder)
        state.setdefault("outputs", {})["timestamps"] = str(timestamp_file)
        state["timestamp_schema"] = TIMESTAMP_SCHEMA
        state["status"] = "completed"
        save_state(state_file, state)

    print("\n全部完成。文件已放回：")
    print(f"  {folder}")
    if normalize_mode(state["mode"]) == MODE_AUDIO:
        print("  - 原声.flac")
        print(f"  - {Path(state['outputs']['audio']).name}")
    else:
        print("  - 原声.mp4")
        print("  - 双语版.mp4")
    print("  - 双语版.srt")
    print("  - 双语版.lrc")
    print("  - 时间戳.txt")
    if project_json.is_file():
        print(f"\nASMR Dubber 工作项目保留在：\n  {project_json.parent}")


def prepare_or_resume(
    paths: ToolPaths,
    config: AppConfig,
    folder: Path,
    mode_argument: str | None,
    *,
    rebuild: bool,
    force: bool,
) -> None:
    if not folder.is_dir():
        raise VideoPreparerError(f"文件夹不存在：{folder}")
    sources = discover_audio(folder)
    state_file = state_path(folder)
    state = None if rebuild else load_state(state_file)
    requested_mode = normalize_mode(mode_argument) if mode_argument else None

    if state is not None and not rebuild:
        state_mode = normalize_mode(state["mode"])
        if requested_mode is not None and requested_mode != state_mode:
            raise VideoPreparerError("现有任务模式与 --mode 不一致；如需更换，请使用 --rebuild。")
        background = discover_background(folder) if state_mode != MODE_AUDIO else None
        current_fingerprint = fingerprint(sources, background)
    else:
        background = None
        current_fingerprint = {}

    if state is not None and not rebuild:
        if state.get("fingerprint") != current_fingerprint:
            print("\n检测到小音频或视频背景在上次任务后发生变化。")
            answer = input("输入 R 按当前文件从头重做；直接按 Enter 退出：").strip().casefold()
            if answer != "r":
                return
            rebuild = True
        missing = missing_resume_artifacts(state, folder) if not rebuild else []
        if not rebuild and missing:
            print("\n旧任务记录依赖的文件已经不存在：")
            for path in missing:
                print(f"  {path}")
            answer = input("输入 R 从头重做；直接按 Enter 退出：").strip().casefold()
            if answer != "r":
                return
            rebuild = True
        if not rebuild and status_at_least(state, "completed"):
            if int(state.get("timestamp_schema") or 0) < TIMESTAMP_SCHEMA:
                print("\n正在补充文件夹名称的中英文信息；不会重跑识别、配音或混音。")
                state["status"] = "outputs_ready"
                save_state(state_file, state)
            else:
                print("\n这个文件夹的任务已经完成。")
                answer = input("输入 R 从头重做；直接按 Enter 退出：").strip().casefold()
                if answer != "r":
                    return
                rebuild = True
        elif not rebuild:
            print(f"\n发现未完成任务，当前阶段：{state.get('status') or '尚未开始'}")
            answer = input("输入 1 继续；输入 2 从头开始：").strip()
            if answer == "2":
                rebuild = True
            elif answer != "1":
                return

    if state is None or rebuild:
        mode = requested_mode or ask_mode(config)
        background = discover_background(folder) if mode != MODE_AUDIO else None
        current_fingerprint = fingerprint(sources, background)
        confirm_overwrite(folder, mode, force=force)
        workspace = workspace_path(folder)
        safe_reset_workspace(workspace)
        state = create_initial_state(folder, mode, sources, background, config)
        save_state(state_file, state)

    print("\n将按以下顺序处理：")
    for item in sources:
        print(f"  {item.order:>4}  {item.path.name}")
    if normalize_mode(state["mode"]) != MODE_AUDIO:
        print(f"背景：{background.name if background else '未找到 null 图片，使用黑色背景'}")
    print(f"模式：{mode_label(state['mode'])}")
    execute_task(paths, folder, state_file, state, sources)


def media_duration_seconds(paths: ToolPaths, media: Path) -> float:
    command = [
        str(paths.ffprobe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise VideoPreparerError(f"无法取得媒体时长：{media}")
    return float(result.stdout.strip())


def video_keyframe_times(paths: ToolPaths, media: Path) -> list[float]:
    command = [
        str(paths.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,flags",
        "-of",
        "json",
        str(media),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise VideoPreparerError(f"无法检查视频关键帧：{media}")
    try:
        packets = json.loads(result.stdout).get("packets") or []
        return [
            float(packet["pts_time"])
            for packet in packets
            if "K" in str(packet.get("flags") or "")
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoPreparerError(f"关键帧信息无效：{media}") from exc


def self_test(paths: ToolPaths) -> None:
    print("运行轻量自检；不会调用 ASR、TTS 或网络……")
    test_harmonized_volume_db = -DEFAULT_HARMONIZED_VOLUME_REDUCTION_DB
    with tempfile.TemporaryDirectory(prefix="video-preparer-test-") as temporary:
        root = Path(temporary)
        sources = (
            ("1 开场.wav", "sine=frequency=440:duration=0.60", "44100", "1"),
            ("2 囁き.flac", "sine=frequency=660:duration=0.70", "48000", "2"),
            ("10 終了.m4a", "sine=frequency=880:duration=0.80", "32000", "1"),
        )
        for name, generator, rate, channels in sources:
            run_ffmpeg(
                paths,
                [
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    generator,
                    "-ar",
                    rate,
                    "-ac",
                    channels,
                    str(root / name),
                ],
            )
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=navy:s=641x479",
                "-frames:v",
                "1",
                str(root / "null.png"),
            ],
        )

        discovered = discover_audio(root)
        if [item.order for item in discovered] != [1, 2, 10]:
            raise VideoPreparerError("自检失败：数字排序错误。")
        background = discover_background(root)
        if background is None:
            raise VideoPreparerError("自检失败：没有识别 null 图片。")
        smart_scan = scan_work(root, excluded_directories=("AutoFlow输出",))
        if smart_scan.audio_count != 3 or not smart_scan.editions:
            raise VideoPreparerError("自检失败：智能扫描没有识别传统数字音轨。")
        smart_sources = [
            source_from_candidate(index, item)
            for index, item in enumerate(smart_scan.editions[0].tracks, start=1)
        ]
        if [item.order for item in smart_sources] != [1, 2, 3]:
            raise VideoPreparerError("自检失败：智能扫描输出顺序错误。")
        if (
            len(smart_job_descriptors(root / "out", LAYOUT_MERGED, smart_sources)) != 1
            or len(smart_job_descriptors(root / "out", LAYOUT_SEPARATE, smart_sources)) != 3
            or len(smart_job_descriptors(root / "out", LAYOUT_BOTH, smart_sources)) != 4
        ):
            raise VideoPreparerError("自检失败：三种输出布局的任务规划错误。")
        workspace = root / "work"
        workspace.mkdir()
        master, timeline = normalize_and_concat(paths, discovered, workspace)
        total_samples = sum(int(item["duration_samples"]) for item in timeline)
        if audio_duration_samples(paths, master) != total_samples:
            raise VideoPreparerError("自检失败：母带采样数错误。")
        rebuilt_master, rebuilt_lengths = concat_audio_files(
            paths,
            [Path(str(item["normalized"])) for item in timeline],
            root / "exact-concat",
            expected_samples=[int(item["duration_samples"]) for item in timeline],
            name="self-test",
        )
        if rebuilt_lengths != [int(item["duration_samples"]) for item in timeline] or (
            audio_duration_samples(paths, rebuilt_master) != total_samples
        ):
            raise VideoPreparerError("自检失败：分轨成品的精确二次合并错误。")

        test_settings = root / "settings.txt"
        test_settings.write_text(
            "asmr_dubber_path=.\n"
            "harmonized_volume_reduction_db=12.5\n"
            "harmonized_delay_minutes=7.5\n"
            "timestamp_footer_line_1=测试页脚\n",
            encoding="utf-8",
        )
        parsed_config = load_app_config(test_settings)
        if (
            parsed_config.asmr_root != root.resolve()
            or parsed_config.harmonized_volume_db != -12.5
            or parsed_config.harmonized_delay_seconds != 450
            or parsed_config.timestamp_footer != "测试页脚"
        ):
            raise VideoPreparerError("自检失败：settings.txt 解析错误。")

        catalogue_root = root / "catalogue"
        wav_dir = catalogue_root / "WAV"
        mp3_dir = catalogue_root / "MP3"
        bonus_dir = catalogue_root / "特典"
        wav_dir.mkdir(parents=True)
        mp3_dir.mkdir(parents=True)
        bonus_dir.mkdir(parents=True)
        shutil.copy2(root / "1 开场.wav", wav_dir / "Track０１ 開場.wav")
        shutil.copy2(root / "1 开场.wav", mp3_dir / "Track01 開場.mp3")
        shutil.copy2(root / "2 囁き.flac", bonus_dir / "EX01 おまけ.wav")
        (wav_dir / "Track０１ 開場.wav.vtt").write_text(
            "WEBVTT\n\n00:00.000 --> 00:00.500\n開場\n",
            encoding="utf-8",
        )
        catalogue = scan_work(catalogue_root)
        wav_edition = next(
            (item for item in catalogue.editions if item.extension == ".wav" and item.directory == "WAV"),
            None,
        )
        if (
            wav_edition is None
            or wav_edition.tracks[0].order_key[1] != 1
            or wav_edition.tracks[0].transcript is None
        ):
            raise VideoPreparerError("自检失败：全角 Track 编号或 VTT 自动匹配错误。")
        _, selected_with_bonus, _ = choose_tracks(
            catalogue,
            parsed_config,
            edition_argument=wav_edition.id,
            include_bonus=True,
        )
        if len(selected_with_bonus) != 2 or selected_with_bonus[-1].category != "bonus":
            raise VideoPreparerError("自检失败：独立特典目录没有加入所选版本。")

        audio_output_folder = root / "audio-mode"
        audio_output_folder.mkdir()
        audio_state = create_initial_state(
            audio_output_folder,
            MODE_AUDIO,
            discovered,
            None,
            AppConfig(
                asmr_root=None,
                harmonized_volume_db=-10.0,
                harmonized_delay_seconds=1_200,
                timestamp_footer="",
            ),
        )
        audio_state["workspace"] = str(root / "audio-mode-work")
        prepare_media_phase(paths, audio_output_folder, audio_state, discovered)
        audio_original = audio_output_folder / "原声.flac"
        if (
            audio_state["status"] != "media_ready"
            or not audio_original.is_file()
            or audio_duration_samples(paths, audio_original) != total_samples
        ):
            raise VideoPreparerError("自检失败：纯音频模式输出错误。")

        fake_project = root / "audio-project"
        (fake_project / "output").mkdir(parents=True)
        (fake_project / "subtitles").mkdir()
        mixed_wav = fake_project / "output" / "mixed.wav"
        run_ffmpeg(
            paths,
            [
                "-loglevel",
                "error",
                "-i",
                str(audio_original),
                "-c:a",
                "pcm_s16le",
                str(mixed_wav),
            ],
        )
        (fake_project / "subtitles" / "bilingual.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n测试\n",
            encoding="utf-8",
        )
        (fake_project / "subtitles" / "bilingual.lrc").write_text(
            "[00:00.00]测试\n",
            encoding="utf-8",
        )
        fake_project_json = fake_project / "project.json"
        fake_project_json.write_text(
            json.dumps(
                {
                    "output_file": "output/mixed.wav",
                    "subtitle_srt_file": "subtitles/bilingual.srt",
                    "subtitle_lrc_file": "subtitles/bilingual.lrc",
                }
            ),
            encoding="utf-8",
        )
        audio_final_folder = root / "audio-final"
        audio_outputs = copy_final_outputs(
            paths,
            fake_project_json,
            audio_final_folder,
            MODE_AUDIO,
            harmonized_delay_seconds=1_200,
            harmonized_volume_db=-10.0,
        )
        if not all(Path(path).is_file() for path in audio_outputs.values()):
            raise VideoPreparerError("自检失败：纯音频模式最终文件回写错误。")

        normal = root / "normal.mp4"
        black = root / "black.mp4"
        harmony = root / "harmony.mp4"
        render_static_video(paths, master, background, normal)
        render_static_video(paths, master, None, black)
        render_static_video(
            paths,
            master,
            background,
            harmony,
            lead_seconds=KEYFRAME_INTERVAL_SECONDS,
            volume_db=test_harmonized_volume_db,
        )
        normal_duration = media_duration_seconds(paths, normal)
        black_duration = media_duration_seconds(paths, black)
        harmony_duration = media_duration_seconds(paths, harmony)
        if not (1.9 <= normal_duration <= 2.3):
            raise VideoPreparerError(f"自检失败：普通视频时长异常 {normal_duration:.3f}s")
        if not (1.9 <= black_duration <= 2.3):
            raise VideoPreparerError(f"自检失败：黑色背景视频时长异常 {black_duration:.3f}s")
        if not (
            normal_duration + KEYFRAME_INTERVAL_SECONDS - 0.2
            <= harmony_duration
            <= normal_duration + KEYFRAME_INTERVAL_SECONDS + 0.2
        ):
            raise VideoPreparerError(
                f"自检失败：前置静音时长异常 {harmony_duration - normal_duration:.3f}s"
            )
        keyframes = video_keyframe_times(paths, harmony)
        if len(keyframes) < 2 or not any(
            abs(timestamp - KEYFRAME_INTERVAL_SECONDS) <= 0.1 for timestamp in keyframes
        ):
            raise VideoPreparerError(f"自检失败：没有按 10 秒间隔写入关键帧：{keyframes}")

        sample_srt = "1\n00:00:01,250 --> 00:00:02,500\n测试\n"
        shifted = shift_srt_text(sample_srt, 1_200_000)
        if "00:20:01,250 --> 00:20:02,500" not in shifted:
            raise VideoPreparerError("自检失败：SRT 偏移错误。")
        shifted_file = root / "shifted.srt"
        shifted_file.write_text(shifted, encoding="utf-8")
        second_srt = root / "second.srt"
        second_srt.write_text(
            "99\n00:00:00,100 --> 00:00:00,300\n第二段\n",
            encoding="utf-8",
        )
        combined_srt = combine_srt_files(
            ((shifted_file, -1_200_000, 2_000), (second_srt, 2_000, 3_000)),
            root / "combined.srt",
        ).read_text(encoding="utf-8-sig")
        if (
            "1\n00:00:01,250 --> 00:00:02,000" not in combined_srt
            or "2\n00:00:02,100" not in combined_srt
        ):
            raise VideoPreparerError("自检失败：多文件 SRT 合并或重新编号错误。")
        delayed = root / "delayed.mp4"
        render_delayed_existing_video(
            paths,
            normal,
            delayed,
            lead_seconds=2,
            subtitle_file=shifted_file,
            volume_db=test_harmonized_volume_db,
            audio_source=master,
        )
        delayed_duration = media_duration_seconds(paths, delayed)
        if not (normal_duration + 1.8 <= delayed_duration <= normal_duration + 2.2):
            raise VideoPreparerError(
                f"自检失败：双语视频前导时长异常 {delayed_duration - normal_duration:.3f}s"
            )
        sample_lrc = "[00:01.25]测试\n"
        if "[20:01.25]" not in shift_lrc_text(sample_lrc, 1_200_000):
            raise VideoPreparerError("自检失败：LRC 偏移错误。")
        first_lrc = root / "first.lrc"
        second_lrc = root / "second.lrc"
        first_lrc.write_text("[ar:测试]\n[00:01.25]第一段\n", encoding="utf-8")
        second_lrc.write_text("[ar:忽略]\n[00:00.10]第二段\n", encoding="utf-8")
        combined_lrc = combine_lrc_files(
            ((first_lrc, 0), (second_lrc, 2_000)),
            root / "combined.lrc",
        ).read_text(encoding="utf-8-sig")
        if "[ar:测试]" not in combined_lrc or "[00:02.10]第二段" not in combined_lrc:
            raise VideoPreparerError("自检失败：多文件 LRC 合并错误。")
        timestamp_state = {
            "source_folder": str(root / "日本語作品"),
            "mode": MODE_VIDEO_NORMAL,
            "timeline": timeline,
            "title_translations": {
                str(item["filename"]): f"中文曲目{index}"
                for index, item in enumerate(timeline, start=1)
            },
            "folder_name_original": "日本語作品",
            "folder_name_translation": "中文作品",
        }
        timestamp_text = write_timestamp_document(timestamp_state, root).read_text(
            encoding="utf-8-sig"
        )
        if (
            "中文名称：中文作品" not in timestamp_text
            or "原始名称：日本語作品" not in timestamp_text
        ):
            raise VideoPreparerError("自检失败：时间戳文档缺少文件夹中英文名称。")
    print("自检通过。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ASMR-Dubber AutoFlow 音频与静态视频双语制作工作流")
    parser.add_argument("folder", nargs="?", help="解压后的作品文件夹")
    parser.add_argument(
        "--scan",
        choices=("smart", "legacy"),
        default="smart",
        help="smart=递归识别 DLsite 作品目录；legacy=兼容旧版根目录数字音轨流程",
    )
    parser.add_argument(
        "--mode",
        choices=("audio", "video-normal", "video-harmonized", "normal", "harmonized"),
        help="audio=纯音频；video-normal=普通视频；video-harmonized=和谐视频",
    )
    parser.add_argument("--rebuild", action="store_true", help="丢弃此文件夹的旧任务状态并从头开始")
    parser.add_argument("--force", action="store_true", help="不询问是否替换已有成品")
    parser.add_argument(
        "--layout",
        choices=("merged", "separate", "both"),
        help="smart 扫描的成品组织：merged=合并、separate=分轨、both=分轨+合并",
    )
    parser.add_argument("--edition", help="smart 扫描时指定版本编号或处理清单中的版本 ID")
    parser.add_argument("--include-bonus", action="store_true", help="smart 扫描时把特典/样本也加入任务")
    parser.add_argument("--output-root", help="smart 扫描的输出目录；默认是源文件夹下的 AutoFlow输出")
    parser.add_argument("--self-test", action="store_true", help="只运行几秒钟的本地媒体自检")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    print_header()
    try:
        config = load_app_config()
        paths = find_tool_paths(config)
        validate_asmr_version()
        if args.self_test:
            self_test(paths)
            return 0
        folder = clean_user_path(args.folder) if args.folder else clean_user_path(
            input("请粘贴解压后的作品文件夹路径：")
        )
        if args.scan == "legacy":
            if args.layout or args.edition or args.include_bonus or args.output_root:
                raise VideoPreparerError("legacy 扫描不支持 --layout、--edition、--include-bonus 或 --output-root。")
            prepare_or_resume(
                paths,
                config,
                folder,
                args.mode,
                rebuild=args.rebuild,
                force=args.force,
            )
        else:
            execute_smart_plan(
                paths,
                config,
                folder,
                mode_argument=args.mode,
                layout_argument=args.layout,
                edition_argument=args.edition,
                include_bonus=args.include_bonus,
                output_root_argument=args.output_root,
                rebuild=args.rebuild,
                force=args.force,
            )
        return 0
    except KeyboardInterrupt:
        log_event("用户取消任务")
        print("\n已取消。完整成品不会被半成品覆盖，已有任务状态会保留。")
        return 130
    except VideoPreparerError as exc:
        log_event(f"操作失败：{exc}")
        print(f"\n操作失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log_event(f"未预期错误：{type(exc).__name__}: {exc}")
        append_log(traceback.format_exc())
        print(f"\n未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
