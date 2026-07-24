import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIGURED_MODEL_PATH = Path(os.getenv("VOSK_MODEL_PATH", "model"))
if not CONFIGURED_MODEL_PATH.is_absolute():
    CONFIGURED_MODEL_PATH = BASE_DIR / CONFIGURED_MODEL_PATH

TARGET_SAMPLE_RATE = 16000
USE_COMMAND_GRAMMAR = os.getenv("VOSK_USE_GRAMMAR", "1").lower() not in {"0", "false", "no", "off"}
_model = None


class VoiceRecognitionError(RuntimeError):
    pass


def _looks_like_vosk_model(path):
    path = Path(path)
    return path.is_dir() and not _missing_model_parts(path)


def _missing_model_parts(path):
    path = Path(path)
    if not path.is_dir():
        return ["model directory"]
    missing = []
    for name in ("am", "conf", "graph"):
        child = path / name
        if not child.is_dir():
            missing.append(name)
            continue
        try:
            if not any(child.iterdir()):
                missing.append(f"{name}/*")
        except OSError:
            missing.append(f"{name}/*")
    return missing


def _resolve_model_path(path):
    path = Path(path)
    if _looks_like_vosk_model(path):
        return path
    if not path.exists() or not path.is_dir():
        return path

    candidates = []
    for child in sorted(path.iterdir()):
        if _looks_like_vosk_model(child):
            candidates.append(child)
    if candidates:
        return candidates[0]
    return path


MODEL_PATH = _resolve_model_path(CONFIGURED_MODEL_PATH)


def _build_command_grammar():
    phrases = {
        "开灯", "打开灯", "把灯打开", "关灯", "关闭灯", "把灯关了",
        "开空调", "打开空调", "启动空调", "关空调", "关闭空调", "空调关机",
        "开制冷", "打开制冷", "制冷模式", "开冷气", "开冷风",
        "开制热", "打开制热", "制热模式", "开暖气", "开暖风",
        "开除湿", "打开除湿", "除湿模式", "开送风", "打开送风", "送风模式",
        "查环境", "查看环境", "环境怎么样", "现在温度多少", "现在湿度多少",
        "风速自动", "自动风", "低风", "中风", "高风",
        "风速调低", "风速调中", "风速调高",
        "风速调成自动", "风速调成低", "风速调成中", "风速调成高",
        "风速调到自动", "风速调到低", "风速调到中", "风速调到高",
    }

    modes = ["制冷", "制热", "除湿", "送风"]
    winds = ["自动", "低", "中", "高"]
    for mode in modes:
        for wind in winds:
            phrases.add(f"开{mode}并且风速调成{wind}")
            phrases.add(f"风速调成{wind}并且开{mode}")
            phrases.add(f"打开{mode}并把风速调成{wind}")

    for temp in range(16, 31):
        phrases.add(f"温度调到{temp}度")
        phrases.add(f"把温度调到{temp}度")
        phrases.add(f"调到{temp}度")
        for mode in ["制冷", "制热"]:
            phrases.add(f"开{mode}并把温度调到{temp}度")

    for delay in ["一分钟", "两分钟", "三分钟", "五分钟", "十分钟", "十五分钟", "半小时", "一小时"]:
        for command in ["开灯", "关灯", "开空调", "关空调", "开制冷", "开制热", "开除湿", "开送风"]:
            phrases.add(f"{delay}后{command}")

    return sorted(phrases)


COMMAND_GRAMMAR = _build_command_grammar()


def speech_engine_status():
    try:
        import vosk  # noqa: F401
        vosk_installed = True
    except ImportError:
        vosk_installed = False

    model_valid = _looks_like_vosk_model(MODEL_PATH)
    missing_parts = _missing_model_parts(MODEL_PATH) if MODEL_PATH.exists() else ["model directory"]
    if model_valid:
        message = "模型完整"
    elif CONFIGURED_MODEL_PATH != MODEL_PATH and not _looks_like_vosk_model(CONFIGURED_MODEL_PATH):
        message = f"配置目录已自动查找到 {MODEL_PATH}，但仍缺少: {', '.join(missing_parts)}"
    else:
        message = f"模型目录缺少: {', '.join(missing_parts)}"
    return {
        "vosk_installed": vosk_installed,
        "configured_model_path": str(CONFIGURED_MODEL_PATH),
        "model_path": str(MODEL_PATH),
        "model_exists": MODEL_PATH.exists(),
        "model_valid": model_valid,
        "model_missing_parts": missing_parts,
        "ffmpeg_installed": shutil.which("ffmpeg") is not None,
        "grammar_enabled": USE_COMMAND_GRAMMAR,
        "grammar_phrases": len(COMMAND_GRAMMAR),
        "message": message,
        "ready": vosk_installed and model_valid,
    }


def _load_model():
    global _model
    if _model is not None:
        return _model

    try:
        import vosk
    except ImportError as exc:
        raise VoiceRecognitionError("未安装 vosk，请先执行 pip install vosk") from exc

    if not MODEL_PATH.exists():
        raise VoiceRecognitionError(f"找不到 Vosk 模型目录: {MODEL_PATH}")
    if not _looks_like_vosk_model(MODEL_PATH):
        raise VoiceRecognitionError(
            f"Vosk模型目录不完整: {MODEL_PATH}。如果模型解压在 model/vosk-model-small-cn-0.22，"
            "系统会自动识别；否则请设置 VOSK_MODEL_PATH 指向包含 am/conf/graph 的目录。"
        )

    vosk.SetLogLevel(-1)
    try:
        _model = vosk.Model(str(MODEL_PATH))
    except Exception as exc:
        raise VoiceRecognitionError(f"Vosk模型加载失败，请检查 model 目录是否是完整中文模型: {exc}") from exc
    return _model


def _new_recognizer(vosk, model, sample_rate):
    if USE_COMMAND_GRAMMAR:
        return vosk.KaldiRecognizer(
            model,
            sample_rate,
            json.dumps(COMMAND_GRAMMAR, ensure_ascii=False),
        )
    return vosk.KaldiRecognizer(model, sample_rate)


def _recognize_wav_path(path):
    model = _load_model()
    try:
        import vosk
    except ImportError as exc:
        raise VoiceRecognitionError("未安装 vosk，请先执行 pip install vosk") from exc
    texts = []
    try:
        wav = wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError) as exc:
        raise VoiceRecognitionError(f"WAV音频读取失败: {exc}") from exc

    with wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise VoiceRecognitionError("音频需要是单声道 16-bit PCM WAV")

        recognizer = _new_recognizer(vosk, model, wav.getframerate())
        while True:
            data = wav.readframes(4000)
            if not data:
                break
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                text = result.get("text", "").replace(" ", "")
                if text:
                    texts.append(text)

        final = json.loads(recognizer.FinalResult())
        final_text = final.get("text", "").replace(" ", "")
        if final_text:
            texts.append(final_text)

    return "".join(texts).strip()


def _is_valid_wav(path):
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnchannels() == 1 and wav.getsampwidth() == 2
    except (wave.Error, EOFError, OSError):
        return False


def _convert_to_wav(input_path):
    if shutil.which("ffmpeg") is None:
        raise VoiceRecognitionError("上传的音频不是可直接识别的 WAV，且系统未安装 ffmpeg，无法转换")

    output_file = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    output_file.close()
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-f",
        "wav",
        output_file.name,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        try:
            os.unlink(output_file.name)
        except OSError:
            pass
        raise VoiceRecognitionError("音频转换超时") from exc

    if completed.returncode != 0:
        try:
            os.unlink(output_file.name)
        except OSError:
            pass
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1] if detail else "未知错误"
        raise VoiceRecognitionError(f"音频转换失败: {message}")

    return Path(output_file.name)


def _suffix_for_upload(filename="", content_type=""):
    filename = filename or ""
    suffix = Path(filename).suffix
    if suffix:
        return suffix
    content_type = (content_type or "").lower()
    if "webm" in content_type:
        return ".webm"
    if "ogg" in content_type:
        return ".ogg"
    if "mp4" in content_type or "m4a" in content_type:
        return ".m4a"
    return ".wav"


def recognize_audio_bytes(data, filename="", content_type=""):
    if not data:
        raise VoiceRecognitionError("没有收到音频数据")

    temp_input = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=_suffix_for_upload(filename, content_type),
    )
    converted_path = None
    try:
        temp_input.write(data)
        temp_input.close()
        input_path = Path(temp_input.name)
        wav_path = input_path if _is_valid_wav(input_path) else _convert_to_wav(input_path)
        if wav_path != input_path:
            converted_path = wav_path
        text = _recognize_wav_path(wav_path)
        return {
            "text": text,
            "engine": "vosk",
            "model_path": str(MODEL_PATH),
            "converted": converted_path is not None,
            "grammar_enabled": USE_COMMAND_GRAMMAR,
        }
    finally:
        try:
            os.unlink(temp_input.name)
        except OSError:
            pass
        if converted_path:
            try:
                os.unlink(converted_path)
            except OSError:
                pass


def recognize_audio_file(path):
    path = Path(path)
    wav_path = path if _is_valid_wav(path) else _convert_to_wav(path)
    try:
        return {
            "text": _recognize_wav_path(wav_path),
            "engine": "vosk",
            "model_path": str(MODEL_PATH),
            "converted": wav_path != path,
            "grammar_enabled": USE_COMMAND_GRAMMAR,
        }
    finally:
        if wav_path != path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass


def audio_callback(indata, frames, time_info, status):
    if status:
        print(status, file=sys.stderr)
    audio_queue.put(bytes(indata))


audio_queue = queue.Queue()


def main():
    try:
        import sounddevice as sd
        import vosk
    except ImportError:
        print(">>> [错误] 录板载麦克风需要安装 sounddevice 和 vosk")
        sys.exit(1)

    _load_model()
    print(">>> [系统] Vosk 语音转文字模式启动")
    print(">>> [状态] 正在监听麦克风，请说话")

    with sd.RawInputStream(
        samplerate=TARGET_SAMPLE_RATE,
        blocksize=8000,
        dtype="int16",
        channels=1,
        callback=audio_callback,
    ):
        recognizer = _new_recognizer(vosk, _load_model(), TARGET_SAMPLE_RATE)

        while True:
            data = audio_queue.get()
            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                user_text = result.get("text", "").replace(" ", "")
                if user_text:
                    print(f"识别结果: {user_text}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n>>> 已停止录音")
