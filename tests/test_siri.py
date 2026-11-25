import struct

from freehand.mic import decode_packets, parse_packet
from freehand.siri import button_changes, parse_touch

# Real packets captured from the remote while holding Siri.
PKT1 = bytes.fromhex(
    "d85b00005eb860b3b474df681db60e6331347c98ada264de97a48625d5cd8ea716aed0ee8929311796100740"
    "8d66c104a267f909e7be89f9d337e8a37302cb260cec3d4ca8a8db0ac68ef6865c6dc2b8acdc7f490e6eda71"
    "4dedaa164dd0bfd219241a"
)
PKT2 = bytes.fromhex(
    "ac03010054b86b5fe0959b2e6041c1262bb3b27e15a52e7dd8e045c88bfba136d4d0b8a0d46d3e1c1b848bed"
    "e081476e44eb9c11324fd5ac800f1f1620baa24ebe590b85f228d71ddf40dd370d0adab15a939dff6bcfbc27"
    "b300000000000000000000"
)


def test_button_diff():
    assert button_changes(0, 0x0020) == [("siri", True)]
    assert button_changes(0x0020, 0) == [("siri", False)]
    # Down + left together, then left released
    assert button_changes(0, 0x1800) == [("down", True), ("left", True)]
    assert button_changes(0x1800, 0x0800) == [("left", False)]


def test_parse_mic_packet():
    seq, frame = parse_packet(PKT1)
    assert seq == 0 and len(frame) == 0x5E and frame[0] == 0xB8
    seq, frame = parse_packet(PKT2)
    assert seq == 1 and len(frame) == 0x54
    assert parse_packet(bytes(99)) is None


def test_decode_real_frames():
    pcm = decode_packets([PKT1, PKT2])
    assert len(pcm) == 2 * 960 * 2  # two 20 ms frames of 16-bit mono at 48 kHz


def test_touch_vectors():
    # Vector from azais-corentin/siri-remote: x=-320, y=-415
    x, y, _ = parse_touch(bytes.fromhex("3214a601c01ee69e8e1aa4"))
    assert (x, y) == (-320, -415)
    assert parse_touch(bytes.fromhex("3214a601000000000000a4")) is None  # no contact
    assert parse_touch(b"\x00" * 11) is None
