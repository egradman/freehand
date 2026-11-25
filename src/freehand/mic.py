"""Record the Siri Remote's microphone and hand the audio to Superwhisper.

Mic report 0xFA (99 bytes): [0:2] junk, [2:4] sequence (LE), [4] Opus frame
length L, [5:5+L] one Opus frame (CELT, 20 ms, 48 kHz mono), rest zero padding.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import os
import shlex
import tempfile
import time
import wave
from pathlib import Path

log = logging.getLogger("freehand")

RATE = 48000
MAX_FRAME_SAMPLES = 5760  # 120 ms at 48 kHz, the Opus maximum
MAX_PLC_FRAMES = 4


class OpusDecoder:
    def __init__(self, rate: int = RATE):
        name = ctypes.util.find_library("opus")
        if not name:
            raise RuntimeError("libopus not found (pacman -S opus)")
        lib = ctypes.CDLL(name)
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        err = ctypes.c_int()
        self.lib = lib
        self.dec = lib.opus_decoder_create(rate, 1, ctypes.byref(err))
        if err.value != 0 or not self.dec:
            raise RuntimeError(f"opus_decoder_create failed ({err.value})")
        self.buf = (ctypes.c_int16 * MAX_FRAME_SAMPLES)()

    def decode(self, frame: bytes | None, samples: int = 960) -> bytes:
        """Decode one frame; frame=None runs packet-loss concealment."""
        if frame is None:
            n = self.lib.opus_decode(self.dec, None, 0, self.buf, samples, 0)
        else:
            n = self.lib.opus_decode(self.dec, frame, len(frame), self.buf, MAX_FRAME_SAMPLES, 0)
        if n < 0:
            raise ValueError(f"opus_decode error {n}")
        return ctypes.string_at(self.buf, n * 2)

    def __del__(self):
        if getattr(self, "dec", None):
            self.lib.opus_decoder_destroy(self.dec)


def parse_packet(data: bytes) -> tuple[int, bytes] | None:
    """-> (sequence, opus frame), or None for padding / end-of-press packets."""
    if len(data) < 6:
        return None
    seq = int.from_bytes(data[2:4], "little")
    n = data[4]
    if n == 0 or n > len(data) - 5:
        return None
    return seq, data[5 : 5 + n]


def decode_packets(packets: list[bytes]) -> bytes:
    dec = OpusDecoder()
    pcm = bytearray()
    last = None
    for raw in packets:
        p = parse_packet(raw)
        if p is None:
            continue
        seq, frame = p
        if last is not None:
            gap = (seq - last - 1) & 0xFFFF
            for _ in range(min(gap, MAX_PLC_FRAMES)):
                pcm += dec.decode(None)
        try:
            pcm += dec.decode(frame)
        except ValueError as e:
            log.debug("bad opus frame %d: %s", seq, e)
        last = seq
    return bytes(pcm)


def write_wav(path: Path, pcm: bytes, rate: int = RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


class RemoteMic:
    """Siri down -> start buffering; Siri up -> decode, save WAV, transcribe."""

    def __init__(self, command: str = "superwhisper transcribe {wav} --insert", min_seconds: float = 0.3):
        self.command = command
        self.min_seconds = min_seconds
        self.recording = False
        self.packets: list[bytes] = []
        self.started = 0.0
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.dir = Path(runtime) / "freehand"

    def audio(self, data: bytes) -> None:
        if self.recording:
            self.packets.append(data)

    def start(self) -> None:
        self.recording, self.packets, self.started = True, [], time.monotonic()
        log.info("recording from the remote's mic")

    def cancel(self) -> None:
        self.recording, self.packets = False, []

    async def finish(self, name: str = "dictation.wav") -> Path | None:
        """End the recording and save it as a WAV; None if there was nothing to keep."""
        if not self.recording:
            return None
        packets = self.packets
        await asyncio.sleep(0.12)  # let the last in-flight packets land
        if self.packets is packets:  # no new recording started meanwhile
            self.recording, self.packets = False, []
        pcm = decode_packets(packets)
        secs = len(pcm) / 2 / RATE
        log.debug("%d mic packets, %.2fs of audio", len(packets), secs)
        if secs < self.min_seconds:
            log.info("recording too short (%.2fs), ignored", secs)
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        wav = self.dir / name
        write_wav(wav, pcm)
        log.info("recorded %.1fs", secs)
        return wav

    async def stop(self) -> bool:
        """Finish the recording; True if something was transcribed."""
        wav = await self.finish()
        if wav is None:
            return False
        cmd = self.command.format(wav=shlex.quote(str(wav)))
        log.info("running %s", cmd)
        proc = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        if proc.returncode:
            log.warning("transcription command exited with %s", proc.returncode)
        self.last_output = out.decode(errors="replace").strip()
        log.debug("transcribe output: %s", self.last_output)
        return True
