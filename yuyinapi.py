import json
import sys

from yuyin import recognize_audio_file, speech_engine_status, VoiceRecognitionError


def main():
    if len(sys.argv) == 1:
        print(json.dumps(speech_engine_status(), ensure_ascii=False, indent=2))
        return

    try:
        result = recognize_audio_file(sys.argv[1])
    except VoiceRecognitionError as exc:
        print(json.dumps({
            "success": False,
            "message": str(exc),
            "status": speech_engine_status(),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    print(json.dumps({
        "success": bool(result["text"]),
        **result,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
