# ASMR-Dubber AutoFlow

[ASMR Dubber 主项目：音频识别、翻译、配音与双语字幕制作](https://github.com/EveningStudy/ASMR-Dubber)

ASMR-Dubber AutoFlow 是 [ASMR Dubber](https://github.com/EveningStudy/ASMR-Dubber) 的批处理助手。给它一个解压后的 DLsite 音声文件夹，它会找出可用音轨、让你确认版本和输出方式，然后依次完成 ASR（语音识别）、翻译、TTS（语音合成）、混音、字幕和视频输出。

它只负责整理和自动执行流程，不自带模型，也不会修改 ASMR Dubber 的代码、全局设置或原作品文件。

当前代码按 ASMR Dubber 0.7.1 的项目格式和命令行接口工作。请使用 0.7.1 或对应的后续兼容版本；早期免安装包不具备这里使用的台本导入和项目级参考接口。

## 能处理什么目录

程序会递归扫描作品文件夹，不要求所有音频都堆在第一层。常见命名都能排序，例如：

```text
01 开场.wav
#2 耳语.wav
Track03 添い寝.flac
トラック４ エピローグ.mp3
04a 左耳.wav
EX01 特典.wav
```

如果作品同时带有 WAV、FLAC、MP3、SE 有/无、纯人声、左右反转或中文目录，程序会把它们分成不同版本供你选择，不会把同一内容的几个版本全混在一起。特典、样本、Free Talk 和闹钟音轨会单独提示。

同名台本或字幕会自动配对。`01.wav.vtt`、`01.srt`、`01.txt` 这类文件都可以识别：

- 日文或英文台本/字幕可以直接交给 ASMR Dubber，能省掉的 ASR 就不再重复运行。
- 有时间轴的中文字幕会保留原文识别结果，只覆盖中文列。
- 无时间轴的中文台本可以直接作为中文配音稿。
- PDF 会列入扫描结果，但不会擅自解析。

## 程序里的选项

粘贴作品文件夹后，程序会先让你确认音频版本以及是否包含特典，然后依次显示下面两组菜单。

第一组菜单决定输出音频还是视频：

```text
请选择处理类型：
  1. 纯音频模式（不制作视频，输出音频与字幕）
  2. 静态视频模式（下一步选择普通或和谐）
```

选择静态视频后，还会继续选择视频分支：

```text
请选择视频分支：
  1. 普通模式（静态背景 + 原音量）
  2. 和谐模式（成品音量 -10 dB，视频与字幕延后 20 分钟）
```

普通视频固定为 1920×1080、5 FPS。和谐模式具体降低多少分贝、延后多少分钟，以 `settings.txt` 为准。

第二组菜单才决定是否拼接：

```text
请选择成品组织方式：
  1. 合并成一部
  2. 每条音轨分别输出（不拼接）
  3. 分轨输出 + 合并版
```

- `合并成一部`：先拼接选中的原音轨，再建立一个 ASMR Dubber 项目。
- `每条音轨分别输出（不拼接）`：每条音轨建立独立项目。
- `分轨输出 + 合并版`：先完成所有分轨，再直接用分轨成品生成合并版，不会把 ASR 和 TTS 重跑第二遍。

所以，“纯音频模式”本身不代表一定拼接。纯音频、普通视频和和谐视频都可以搭配这三种成品组织方式。

分轨任务会先处理最长的一条音轨。你只需要选择一次统一音色参考，程序会把它固化为本作品的共用参考，后面的音轨自动复用。五分钟内没有手动选择时，继续使用 ASMR Dubber 推荐的默认参考片段。

和谐模式下，每个独立分轨视频都有自己的前置无声画面；合并版只在整部作品开头增加一次。

## 使用方法

1. 先确认 ASMR Dubber 已经安装完成，网页中的 ASR、翻译和 TTS 设置能正常使用。
2. 打开 `settings.txt`，把 `asmr_dubber_path` 改成 ASMR Dubber 所在目录。
3. 双击 `ASMR-Dubber-AutoFlow.cmd`。
4. 粘贴解压后的作品文件夹路径。
5. 按提示依次确认音频版本、特典、处理类型、视频分支（如果选择视频）和成品组织方式。
6. 网页打开后，点击“打开项目”，选择一段清晰音频并点击“设为项目音色参考”。如果还改了校对表格，也要保存表格。
7. 回到命令行等待完成。

ASMR Dubber 当前保存的识别模型、翻译服务、API 密钥、配音模型和混音参数都会沿用。AutoFlow 不改全局设置；它只会在自己创建的项目里保存共用参考，并在用户原本只输出中文干声时同时保留双语混音，以便生成“双语版”。

启动脚本使用 Windows 自带的 PowerShell 5.1，不要求另外安装 PowerShell 7。

## 输出位置

默认输出到作品文件夹里的 `AutoFlow输出`：

```text
AutoFlow输出/
├─ 处理清单.json
├─ 曲目清单.txt
├─ 总时间戳.txt
├─ 分轨/
│  ├─ 001 开场/
│  │  ├─ 原声.flac 或 原声.mp4
│  │  ├─ 双语版.wav/flac 或 双语版.mp4
│  │  ├─ 双语版.srt
│  │  ├─ 双语版.lrc
│  │  └─ 时间戳.txt
│  └─ ...
└─ 合并版/
   ├─ 原声.flac 或 原声.mp4
   ├─ 双语版.flac 或 双语版.mp4
   ├─ 双语版.srt
   ├─ 双语版.lrc
   └─ 时间戳.txt
```

只选择一种组织方式时，不需要的目录不会建立。`总时间戳.txt` 按原曲目顺序计算；分轨文件本身仍各自从 `00:00` 开始。

任务状态和临时文件保存在 AutoFlow 程序目录下的 `.state` 与 `.work`。中断后再次运行同一任务会继续；整项任务成功后会清掉不再需要的 `.work` 副本。输入 `Ctrl+C` 可以取消当前命令，已经完成的成品和状态会保留。日志在 `.state/autoflow.log`。

## 设置文件

`settings.txt` 中常用的项目如下：

```text
asmr_dubber_path=..\asmr-next
output_folder_name=AutoFlow输出
default_output_layout=ask
preferred_audio_formats=wav,flac,ape,m4a,mp3
bonus_policy=ask
background_policy=auto
harmonized_volume_reduction_db=10
harmonized_delay_minutes=20
```

- `default_output_layout`：`ask`、`merged`、`separate` 或 `both`。
- `bonus_policy`：`ask`、`include` 或 `exclude`。
- `background_policy`：`auto` 自动选择 `null`、封面或体积较大的图片；`black` 始终使用黑色。
- `timestamp_footer_line_1` 至 `timestamp_footer_line_5`：写入时间戳文档末尾，留空即可删除。

修改设置只影响新任务。已经开始的任务会继续使用创建时保存的音量、延后时间和参考音频；需要全部重做时使用 `--rebuild`。

## 命令行

```powershell
# 默认智能扫描，运行时询问模式和布局
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹"

# 纯音频，分轨和合并都输出
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --mode audio --layout both

# 普通静态视频，只输出分轨
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --mode video-normal --layout separate

# 和谐静态视频，只输出合并版
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --mode video-harmonized --layout merged

# 指定扫描列表里的版本并包含特典
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --edition 2 --include-bonus

# 丢弃这一计划的 AutoFlow 成品和状态后重做
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --rebuild

# 旧版兼容：只读取根目录中数字开头的音轨，并把成品写回原目录
ASMR-Dubber-AutoFlow.cmd "D:\作品文件夹" --scan legacy

# 几秒钟的离线媒体自检，不运行 ASR/TTS，不访问网络
ASMR-Dubber-AutoFlow.cmd --self-test
```

视频背景保持原图比例并补黑边，不会把图片拉伸，也不会为了静态画面生成高码率视频。双语视频优先烧录可见字幕；如果本机 FFmpeg 缺少字幕渲染能力，会退回到可选择的内嵌字幕轨，同时保留外部 SRT/LRC。音频统一为 48 kHz 双声道；中间拼接使用无损 FLAC，视频音频使用 AAC 256 kbps。
