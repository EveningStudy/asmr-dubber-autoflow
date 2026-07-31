# ASMR-Dubber AutoFlow

ASMR-Dubber AutoFlow 是一个配合 ASMR Dubber 使用的轻量命令行工具。它负责按编号拼接一组音频，并自动建立 ASMR Dubber 项目、运行语音识别和翻译，最后生成配音、字幕与时间戳。

目前提供 Windows 命令行入口。

## 三种模式

1. **纯音频模式**
   - 按编号拼接音频，输出无损的 `原声.flac`。
   - 将拼接音频交给 ASMR Dubber。
   - 最终输出 `双语版.wav`、SRT、LRC 和 `时间戳.txt`。

2. **视频模式 · 普通**
   - 使用 `null.jpg`、`null.png` 等图片作为静态背景；没有图片时使用黑色背景。
   - 输出 `原声.mp4`，再由 ASMR Dubber 生成双语视频和字幕。

3. **视频模式 · 和谐**
   - 处理流程与普通视频相同。
   - 最终视频降低指定音量，并在开头增加指定时长的无声静态画面。
   - 视频、字幕和时间戳会使用相同的时间偏移。

视频固定为 1920×1080、5 FPS。背景图保持比例并补黑边，只缩放一次，不会反复解码大图。音频保持 48 kHz、双声道、AAC 256 kbps。

## 使用前设置

打开 `settings.txt`：

```text
asmr_dubber_path=..\asmr-next
harmonized_volume_reduction_db=10
harmonized_delay_minutes=20
```

- `asmr_dubber_path`：ASMR Dubber 的项目根目录。
- `harmonized_volume_reduction_db`：和谐视频降低的分贝数，填写正数。
- `harmonized_delay_minutes`：和谐视频整体延后的分钟数，可以填写小数。
- `timestamp_footer_line_1` 至 `timestamp_footer_line_5`：`时间戳.txt` 末尾的自定义文字，留空即可删除对应行。

ASMR Dubber 必须已经安装完成，并且能够正常运行。识别模型、配音模型、翻译服务和 API 密钥都沿用 ASMR Dubber 当前的用户设置。

## 准备文件

把小音频放在同一个文件夹第一层，文件名以数字开头：

```text
1 开场.mp3
2 正文.flac
10 结束.m4a
```

程序按开头数字排序，因此顺序是 `1、2、10`。支持 WAV、FLAC、MP3、M4A、AAC、OGG、Opus、WMA、MKA、M4B 和 APE。

视频模式可放入一张 basename 为 `null` 的静态图片，例如 `null.jpg`。支持 PNG、JPG、JPEG、WebP、BMP、TIF 和 TIFF。

## 操作步骤

1. 双击 `ASMR-Dubber-AutoFlow.cmd`。
2. 粘贴包含小音频的文件夹路径。
3. 选择纯音频、普通视频或和谐视频模式。
4. 等待程序完成拼接、ASR（语音识别）和日文翻译。
5. 网页打开后，点击“打开项目”，选择一段清晰音频并点击“设为项目音色参考”。
6. 程序检测到保存结果后自动继续。5 分钟没有手动选择时，使用 ASMR Dubber 推荐的默认参考片段。
7. 等待 TTS（语音合成）、混音、字幕和时间戳完成。

`时间戳.txt` 会包含文件夹名称的中文翻译与原名，以及每段音频的开始时间、中日文标题。

任务状态和中间文件保存在工具目录的 `.state` 与 `.work` 中。任务中断后，再次输入同一文件夹即可继续；源音频或背景发生变化时，程序会提示从头重做。

修改 `settings.txt` 后，新任务会使用新设置；已经开始的任务继续使用创建时保存的参数。如需让旧任务采用新参数，请选择从头重做。

## 命令行用法

```powershell
ASMR-Dubber-AutoFlow.cmd "D:\音声文件夹" --mode audio
ASMR-Dubber-AutoFlow.cmd "D:\音声文件夹" --mode video-normal
ASMR-Dubber-AutoFlow.cmd "D:\音声文件夹" --mode video-harmonized
ASMR-Dubber-AutoFlow.cmd "D:\音声文件夹" --rebuild
ASMR-Dubber-AutoFlow.cmd --self-test
```

本工具不会修改 ASMR Dubber 的代码或全局用户设置，只调用它提供的现有命令和本机已保存的密钥。
