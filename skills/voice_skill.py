import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
import re

import numpy as np
import sounddevice as sd
import soundfile as sf
import speech_recognition as sr


VOICE_DIR = Path(__file__).resolve().parent.parent / "voice_tmp"
VOICE_EXIT_PHRASES = {
    "退出对话",
    "退出语音对话",
    "结束对话",
    "结束语音对话",
    "停止对话",
    "停止语音对话",
    "退出",
    "结束",
    "stop conversation",
    "exit conversation",
    "quit conversation",
}


class VoiceState(Enum):
    IDLE = auto()
    LISTENING = auto()
    TRANSCRIBING = auto()
    THINKING = auto()
    SPEAKING = auto()
    EXITING = auto()


@dataclass
class VoiceTurnResult:
    audio_path: Path | None = None
    transcript: str = ""
    message: str = ""
    no_input: bool = False


def _voice_file_path(file_path: str | None = None):
    if file_path:
        target = Path(file_path).expanduser()
    else:
        filename = datetime.now().strftime("voice_%Y%m%d_%H%M%S.wav")
        target = VOICE_DIR / filename

    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _should_exit_voice_mode(transcript: str):
    normalized = " ".join(transcript.strip().lower().split())
    return normalized in VOICE_EXIT_PHRASES


def _audio_samples_to_audio_data(samples: np.ndarray, sample_rate: int):
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    return sr.AudioData(pcm.tobytes(), sample_rate, 2)


def _recognize_audio_data(audio_data: sr.AudioData, language: str):
    recognizer = sr.Recognizer()
    try:
        return recognizer.recognize_google(audio_data, language=language).strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as exc:
        return f"[Speech service error: {exc}]"


def _transcribe_audio_path(target: Path, language: str):
    recognizer = sr.Recognizer()
    normalized_target = target
    try:
        with sr.AudioFile(str(normalized_target)) as source:
            audio_data = recognizer.record(source)
    except ValueError:
        audio_array, sample_rate = sf.read(str(target), dtype="float32")
        if audio_array.ndim > 1:
            audio_array = np.mean(audio_array, axis=1)

        normalized_target = _voice_file_path()
        sf.write(str(normalized_target), audio_array, sample_rate, subtype="PCM_16")
        with sr.AudioFile(str(normalized_target)) as source:
            audio_data = recognizer.record(source)

    try:
        return recognizer.recognize_google(audio_data, language=language).strip(), ""
    except sr.UnknownValueError:
        return "", f"Could not understand audio from: {target}"
    except sr.RequestError as exc:
        return "", f"Speech recognition service error: {exc}"


def _record_until_silence(
    target: Path,
    language: str,
    sample_rate: int,
    silence_seconds: float,
    wait_for_speech_seconds: float,
    partial_update_seconds: float,
    volume_threshold: float,
):
    frames = []
    state = {
        "speech_started": False,
        "last_voice_time": None,
        "start_time": time.time(),
    }
    partial_text = {"value": ""}
    partial_lock = threading.Lock()
    last_partial_check = 0.0

    def callback(indata, frames_count, time_info, status):
        if status:
            print(f"\n[Voice status] {status}")

        mono = indata[:, 0].copy()
        frames.append(mono)
        volume = float(np.sqrt(np.mean(np.square(mono)))) if len(mono) else 0.0
        now = time.time()
        if volume >= volume_threshold:
            state["speech_started"] = True
            state["last_voice_time"] = now

    try:
        print("[Voice] Listening... Start speaking.")
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", callback=callback):
            while True:
                now = time.time()

                if not state["speech_started"]:
                    if now - state["start_time"] >= wait_for_speech_seconds:
                        return VoiceTurnResult(message="No speech detected.", no_input=True)
                    time.sleep(0.05)
                    continue

                if now - last_partial_check >= partial_update_seconds and frames:
                    last_partial_check = now
                    samples = np.concatenate(frames, axis=0)
                    if len(samples) >= int(sample_rate * 0.8):
                        recognized = _recognize_audio_data(_audio_samples_to_audio_data(samples, sample_rate), language)
                        if recognized and not recognized.startswith("[Speech service error:"):
                            with partial_lock:
                                if recognized != partial_text["value"]:
                                    partial_text["value"] = recognized
                                    print(f"\r[Voice Text] {recognized}", end="", flush=True)

                if state["last_voice_time"] and now - state["last_voice_time"] >= silence_seconds:
                    break

                time.sleep(0.05)

        samples = np.concatenate(frames, axis=0) if frames else np.array([], dtype="float32")
        if samples.size == 0:
            return VoiceTurnResult(message="No speech detected.", no_input=True)

        sf.write(str(target), samples, sample_rate, subtype="PCM_16")
        print()
        return VoiceTurnResult(audio_path=target)
    except Exception as exc:
        return VoiceTurnResult(
            message=(
                "Microphone recording failed. Check macOS microphone permissions and audio device availability. "
                f"Details: {exc}"
            )
        )


class VoiceConversationStateMachine:
    def __init__(
        self,
        agent=None,
        language: str = "zh-CN",
        voice: str = "",
        rate: int = 180,
        sample_rate: int = 16000,
        silence_seconds: float = 2.0,
        wait_for_speech_seconds: float = 2.0,
        partial_update_seconds: float = 1.0,
        volume_threshold: float = 0.015,
    ):
        self.agent = agent
        self.language = language
        self.voice = voice
        self.rate = rate
        self.sample_rate = sample_rate
        self.silence_seconds = silence_seconds
        self.wait_for_speech_seconds = wait_for_speech_seconds
        self.partial_update_seconds = partial_update_seconds
        self.volume_threshold = volume_threshold
        self.state = VoiceState.IDLE
        self.rounds = []
        self.consecutive_no_input = 0

    def transition_to(self, state: VoiceState):
        self.state = state

    def _split_reply_for_speech(self, text: str):
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []

        segments = re.split(r"(?<=[。！？!?；;\n])\s*", normalized)
        return [segment.strip() for segment in segments if segment.strip()]

    def _speak(self, text: str):
        previous_state = self.state
        self.transition_to(VoiceState.SPEAKING)
        try:
            return speak_text(text, voice=self.voice, rate=self.rate)
        finally:
            if previous_state == VoiceState.EXITING:
                self.transition_to(VoiceState.EXITING)
            else:
                self.transition_to(VoiceState.IDLE)

    def _display_and_speak_reply(self, text: str):
        chunks = self._split_reply_for_speech(text)
        if not chunks:
            return []

        previous_state = self.state
        self.transition_to(VoiceState.SPEAKING)
        speech_results = []
        try:
            for index, chunk in enumerate(chunks, start=1):
                print(f"[Agent Voice {index}/{len(chunks)}] {chunk}")
                speech_results.append(speak_text(chunk, voice=self.voice, rate=self.rate))
        finally:
            if previous_state == VoiceState.EXITING:
                self.transition_to(VoiceState.EXITING)
            else:
                self.transition_to(VoiceState.IDLE)

        return speech_results

    def listen_once(self, save_path: str = ""):
        target = _voice_file_path(save_path or None)
        self.transition_to(VoiceState.LISTENING)
        result = _record_until_silence(
            target=target,
            language=self.language,
            sample_rate=self.sample_rate,
            silence_seconds=self.silence_seconds,
            wait_for_speech_seconds=self.wait_for_speech_seconds,
            partial_update_seconds=self.partial_update_seconds,
            volume_threshold=self.volume_threshold,
        )
        if result.audio_path is None:
            self.transition_to(VoiceState.IDLE)
            return result

        self.transition_to(VoiceState.TRANSCRIBING)
        transcript, error_message = _transcribe_audio_path(result.audio_path, self.language)
        if error_message:
            self.transition_to(VoiceState.IDLE)
            return VoiceTurnResult(
                audio_path=result.audio_path,
                transcript="",
                message=error_message,
                no_input=result.no_input,
            )

        self.transition_to(VoiceState.IDLE)
        return VoiceTurnResult(
            audio_path=result.audio_path,
            transcript=transcript,
            message=f"Saved audio: {result.audio_path}\nAudio: {result.audio_path}\nTranscript: {transcript}",
        )

    def run_once(self):
        result = self.listen_once()
        if not result.transcript:
            return result.message or "No transcript produced from voice input."

        if self.agent is None:
            return result.message

        if _should_exit_voice_mode(result.transcript):
            self.transition_to(VoiceState.EXITING)
            goodbye = "已退出语音对话。"
            self._speak(goodbye)
            return f"Heard: {result.transcript}\nReply: {goodbye}"

        self.transition_to(VoiceState.THINKING)
        answer = self.agent.run(result.transcript, reset_history=False)
        speech_results = self._display_and_speak_reply(answer)
        return (
            f"Heard: {result.transcript}\n"
            f"Reply: {answer}\n"
            f"Speech: {' | '.join(speech_results)}"
        )

    def run_loop(self):
        greeting = "已进入语音对话模式。请开始说话。说“退出对话”即可退出。"
        self._speak(greeting)

        while self.state != VoiceState.EXITING:
            result = self.listen_once()
            if not result.transcript:
                if result.no_input:
                    self.consecutive_no_input += 1
                    if self.consecutive_no_input >= 2:
                        self.transition_to(VoiceState.EXITING)
                        goodbye = "连续两次没有检测到语音输入，已退出语音对话。"
                        self._speak(goodbye)
                        self.rounds.append(f"Reply: {goodbye}")
                        break

                    prompt_retry = "我没有检测到语音输入，请再说一次。"
                    self.rounds.append(result.message)
                    self._speak(prompt_retry)
                    continue

                self.rounds.append(result.message or "No transcript produced from voice input.")
                self._speak("我没有听清，请再说一次。")
                self.consecutive_no_input = 0
                continue

            self.consecutive_no_input = 0

            if _should_exit_voice_mode(result.transcript):
                self.transition_to(VoiceState.EXITING)
                goodbye = "已退出语音对话。"
                self._speak(goodbye)
                self.rounds.append(f"Heard: {result.transcript}\nReply: {goodbye}")
                break

            if self.agent is None:
                self.rounds.append(result.message)
                continue

            self.transition_to(VoiceState.THINKING)
            answer = self.agent.run(result.transcript, reset_history=False)
            speech_results = self._display_and_speak_reply(answer)
            self.rounds.append(
                f"Heard: {result.transcript}\n"
                f"Reply: {answer}\n"
                f"Speech: {' | '.join(speech_results)}"
            )

        return "\n\n".join(self.rounds)


def speak_text(text: str, voice: str = "", rate: int = 180):
    if not text.strip():
        return "No text to speak."

    command = ["say", "-r", str(rate)]
    if voice:
        command.extend(["-v", voice])
    command.append(text)

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "say failed")

    return f"Spoken successfully: {text[:80]}"


def transcribe_audio_file(file_path: str, language: str = "zh-CN"):
    target = Path(file_path).expanduser()
    if not target.exists():
        raise FileNotFoundError(f"Audio file does not exist: {target}")

    transcript, error_message = _transcribe_audio_path(target, language)
    if error_message:
        return error_message

    return f"Audio: {target}\nTranscript: {transcript}"


def listen_and_transcribe(
    language: str = "zh-CN",
    sample_rate: int = 16000,
    save_path: str = "",
    silence_seconds: float = 2.0,
    wait_for_speech_seconds: float = 2.0,
    partial_update_seconds: float = 1.0,
    volume_threshold: float = 0.015,
):
    machine = VoiceConversationStateMachine(
        language=language,
        sample_rate=sample_rate,
        silence_seconds=silence_seconds,
        wait_for_speech_seconds=wait_for_speech_seconds,
        partial_update_seconds=partial_update_seconds,
        volume_threshold=volume_threshold,
    )
    result = machine.listen_once(save_path=save_path)
    return result.message or "No transcript produced from voice input."


def register(agent):
    def voice_chat_once(language: str = "zh-CN", voice: str = "", rate: int = 180):
        machine = VoiceConversationStateMachine(agent=agent, language=language, voice=voice, rate=rate)
        return machine.run_once()

    def voice_chat_loop(language: str = "zh-CN", voice: str = "", rate: int = 180):
        machine = VoiceConversationStateMachine(agent=agent, language=language, voice=voice, rate=rate)
        return machine.run_loop()

    agent.add_skill(
        name="speak_text",
        func=speak_text,
        description="Speak text aloud using macOS say.",
        parameters={
            "text": "string",
            "voice": "string",
            "rate": "integer",
        },
    )
    agent.add_skill(
        name="transcribe_audio_file",
        func=transcribe_audio_file,
        description="Transcribe an audio file to text using speech recognition.",
        parameters={
            "file_path": "string",
            "language": "string",
        },
    )
    agent.add_skill(
        name="listen_and_transcribe",
        func=listen_and_transcribe,
        description="Listen from the microphone until 2 seconds of silence, show near-real-time transcript text, and transcribe the final utterance.",
        parameters={
            "language": "string",
            "sample_rate": "integer",
            "save_path": "string",
            "silence_seconds": "number",
            "wait_for_speech_seconds": "number",
            "partial_update_seconds": "number",
            "volume_threshold": "number",
        },
    )
    agent.add_skill(
        name="voice_chat_once",
        func=voice_chat_once,
        description="Listen to one utterance, end on silence, transcribe it, ask the agent to reply, and speak the reply aloud.",
        parameters={
            "language": "string",
            "voice": "string",
            "rate": "integer",
        },
    )
    agent.add_skill(
        name="voice_chat_loop",
        func=voice_chat_loop,
        description=(
            "Enter continuous voice conversation mode. The agent keeps listening and replying aloud until the user says 退出对话 or until no speech is detected twice in a row."
        ),
        parameters={
            "language": "string",
            "voice": "string",
            "rate": "integer",
        },
    )