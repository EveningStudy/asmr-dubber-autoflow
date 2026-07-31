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
import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


TOOL_ROOT = Path(__file__).resolve().parent
WORK_ROOT = TOOL_ROOT / ".work"
STATE_ROOT = TOOL_ROOT / ".state"
SETTINGS_FILE = TOOL_ROOT / "settings.txt"

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


def print_header() -> None:
    print()
    print("=" * 68)
    print("  ASMR-Dubber AutoFlow · 音频拼接 / 静态视频 / 双语制作")
    print("=" * 68)


def normalize_mode(value: Any) -> str:
    normalized = MODE_ALIASES.get(str(value or "").strip().casefold())
    if normalized is None:
        raise VideoPreparerError(f"未知模式：{value}")
    return normalized


def mode_label(mode: str) -> str:
    return {
        MODE_AUDIO: "纯音频模式",
        MODE_VIDEO_NORMAL: "视频模式 · 普通",
        MODE_VIDEO_HARMONIZED: "视频模式 · 和谐",
    }[normalize_mode(mode)]


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
    return AppConfig(
        asmr_root=asmr_root,
        harmonized_volume_db=-abs(reduction),
        harmonized_delay_seconds=round(delay_minutes * 60),
        timestamp_footer="\n".join(line for line in footer_lines if line),
    )


def clean_user_path(value: str) -> Path:
    text = value.strip().strip('"').strip("'")
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
        raise VideoPreparerError("asmr-next 尚未安装完整运行环境，找不到便携 Python。")

    ffmpeg_root = asmr_home / "runtimes" / "ffmpeg-shared"
    ffmpeg = next(iter(sorted(ffmpeg_root.rglob("ffmpeg.exe"))), None)
    ffprobe = next(iter(sorted(ffmpeg_root.rglob("ffprobe.exe"))), None)
    if ffmpeg is None or ffprobe is None:
        raise VideoPreparerError("asmr-next 的 FFmpeg/FFprobe 不完整，请先修复其安装。")

    cli_script = asmr_root / "scripts" / "windows" / "run-cli.ps1"
    launcher = asmr_root / "ASMR-Dubber.exe"
    if not cli_script.is_file() or not launcher.is_file():
        raise VideoPreparerError("asmr-next 缺少命令行脚本或启动程序。")

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
                "size": item.size,
                "mtime_ns": item.mtime_ns,
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


def run_process(arguments: list[str], *, cwd: Path | None = None) -> None:
    try:
        result = subprocess.run(arguments, cwd=cwd, check=False)
    except OSError as exc:
        raise VideoPreparerError(f"无法启动命令：{arguments[0]}: {exc}") from exc
    if result.returncode != 0:
        raise VideoPreparerError(
            f"命令执行失败（退出码 {result.returncode}）：{Path(arguments[0]).name}"
        )


def run_process_captured(arguments: list[str], *, cwd: Path | None = None) -> str:
    environment = os.environ.copy()
    environment.update({"COLUMNS": "10000", "NO_COLOR": "1", "TERM": "dumb"})
    try:
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
    if result.stderr:
        print(
            result.stderr,
            end="" if result.stderr.endswith("\n") else "\n",
            file=sys.stderr,
        )
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
                "title_ja": source.title_ja,
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
        raise VideoPreparerError(f"无法读取 asmr-next 用户设置：{exc}") from exc
    return Path(value).expanduser().resolve() if value else (paths.asmr_home / "projects")


def known_project_manifests(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {path.resolve() for path in root.rglob("project.json") if path.is_file()}


def create_asmr_project(paths: ToolPaths, video: Path) -> Path:
    projects_root = configured_projects_root(paths)
    before = known_project_manifests(projects_root)
    started_ns = time.time_ns()
    output = run_asmr_cli_captured(
        paths,
        "create",
        str(video),
        "--projects-root",
        str(projects_root),
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
        raise VideoPreparerError("asmr-next 已返回成功，但没有找到新项目的 project.json。")
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
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            print("5 分钟内未检测到手动选择，将使用 ASMR Dubber 推荐的默认参考音频。")
            return True
        if now >= next_notice:
            print(f"  仍在等待参考音频，剩余约 {max(1, int(remaining // 60) + 1)} 分钟……")
            next_notice = now + 60
        time.sleep(min(2.0, remaining))


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
        raise VideoPreparerError(f"无法加载 asmr-next 的 DeepSeek 翻译工具：{exc}") from exc

    try:
        settings = load_user_settings()
        preset = PROVIDER_PRESETS["deepseek"]
        if settings.translation_provider == "deepseek":
            model = settings.translation_model
            base_url = settings.translation_base_url.strip() or str(preset["base_url"])
            temperature = settings.translation_temperature
            top_p = settings.translation_top_p
            max_tokens = settings.translation_max_output_tokens
        else:
            model = str(preset["default_model"])
            base_url = str(preset["base_url"])
            temperature = 0.1
            top_p = 1.0
            max_tokens = 16_384
        api_key = resolve_api_key("deepseek")
    except Exception as exc:
        raise VideoPreparerError(f"无法读取 asmr-next 的 DeepSeek 设置或密钥：{exc}") from exc

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
        filename = str(item["filename"])
        sentences.append(
            Sentence(
                id=f"title{index:04d}",
                start_seconds=float(index),
                end_seconds=float(index + 1),
                ja_text=str(item["title_ja"]),
                zh_text=cached.get(filename, ""),
            )
        )

    try:
        translate_sentences(
            sentences,
            api_key=api_key,
            provider="deepseek",
            model=model,
            base_url=base_url,
            system_prompt=TITLE_TRANSLATION_PROMPT,
            temperature=temperature,
            top_p=top_p,
            max_output_tokens=max_tokens,
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
                cached[str(item["filename"])] = sentence.zh_text.strip()
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
        if not title:
            missing.append(str(item["filename"]))
            continue
        translated[str(item["filename"])] = title
    state["title_translations"] = translated
    if missing:
        raise VideoPreparerError("以下名称或标题翻译为空：" + "、".join(missing))
    return translated


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
        filename = str(item["filename"])
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
        print("  1. 纯音频模式（拼接音频后交给 ASMR Dubber）")
        print("  2. 静态视频模式")
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
) -> None:
    if not status_at_least(state, "media_ready"):
        prepare_media_phase(paths, folder, state, sources)
        save_state(state_file, state)

    if not status_at_least(state, "project_created"):
        print("\n[3/5] 创建 ASMR Dubber 项目")
        project_json = create_asmr_project(paths, Path(state["dubbing_input"]))
        state["project_json"] = str(project_json)
        state["status"] = "project_created"
        save_state(state_file, state)

    project_json = Path(state["project_json"])
    if not project_json.is_file() and not status_at_least(state, "outputs_ready"):
        raise VideoPreparerError(f"ASMR Dubber 项目已经不存在：{project_json}")

    if not status_at_least(state, "analyzed"):
        print("\n  运行 ASR（语音识别）……")
        run_asmr_cli(paths, "analyze", str(project_json))
        state["status"] = "analyzed"
        save_state(state_file, state)

    if not status_at_least(state, "awaiting_reference"):
        print("\n  翻译日文……")
        run_asmr_cli(paths, "translate", str(project_json))
        state["status"] = "awaiting_reference"
        save_state(state_file, state)

    if not status_at_least(state, "synthesized"):
        reference_id = project_reference_id(project_json)
        if reference_id:
            print(f"\n复用已保存的统一音色参考：{reference_id}")
        elif not wait_for_reference(paths, project_json):
            return
        print("\n[5/5] TTS（语音合成）、混音与字幕")
        run_asmr_cli(paths, "synthesize", str(project_json))
        state["status"] = "synthesized"
        save_state(state_file, state)

    if not status_at_least(state, "mixed"):
        run_asmr_cli(paths, "mix", str(project_json))
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
        print("\n  使用 asmr-next 的 DeepSeek 工具翻译短音频标题……")
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
    state = load_state(state_file)
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
        workspace = root / "work"
        workspace.mkdir()
        master, timeline = normalize_and_concat(paths, discovered, workspace)
        total_samples = sum(int(item["duration_samples"]) for item in timeline)
        if audio_duration_samples(paths, master) != total_samples:
            raise VideoPreparerError("自检失败：母带采样数错误。")

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
    parser = argparse.ArgumentParser(description="ASMR-Dubber AutoFlow 音频拼接与双语制作工作流")
    parser.add_argument("folder", nargs="?", help="包含数字开头小音频的文件夹")
    parser.add_argument(
        "--mode",
        choices=("audio", "video-normal", "video-harmonized", "normal", "harmonized"),
        help="audio=纯音频；video-normal=普通视频；video-harmonized=和谐视频",
    )
    parser.add_argument("--rebuild", action="store_true", help="丢弃此文件夹的旧任务状态并从头开始")
    parser.add_argument("--force", action="store_true", help="不询问是否替换已有成品")
    parser.add_argument("--self-test", action="store_true", help="只运行几秒钟的本地媒体自检")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    print_header()
    try:
        config = load_app_config()
        paths = find_tool_paths(config)
        if args.self_test:
            self_test(paths)
            return 0
        folder = clean_user_path(args.folder) if args.folder else clean_user_path(
            input("请粘贴包含小音频的文件夹路径：")
        )
        prepare_or_resume(
            paths,
            config,
            folder,
            args.mode,
            rebuild=args.rebuild,
            force=args.force,
        )
        return 0
    except KeyboardInterrupt:
        print("\n已取消。完整成品不会被半成品覆盖，已有任务状态会保留。")
        return 130
    except VideoPreparerError as exc:
        print(f"\n操作失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\n未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
