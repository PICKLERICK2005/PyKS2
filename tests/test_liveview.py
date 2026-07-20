"""Live view: the shared MjpegFrameParser and the liveview() context manager."""
import pytest

from pyks2._mjpeg import MjpegFrameParser
from tests.conftest import LIVEVIEW_FRAMES, LIVEVIEW_BODY


# --- pure parser -------------------------------------------------------

def test_mjpeg_frame_parser_single_feed():
    parser = MjpegFrameParser()
    frames = parser.feed(LIVEVIEW_BODY)
    assert frames == LIVEVIEW_FRAMES


def test_mjpeg_frame_parser_split_across_feeds():
    """Frame boundaries (and even SOI/EOI markers themselves) can land split
    across two reads; the parser must still assemble complete frames."""
    parser = MjpegFrameParser()
    frames = []
    for i in range(0, len(LIVEVIEW_BODY), 3):  # tiny, boundary-hostile chunks
        frames.extend(parser.feed(LIVEVIEW_BODY[i:i + 3]))
    assert frames == LIVEVIEW_FRAMES


def test_mjpeg_frame_parser_ignores_empty_chunk():
    parser = MjpegFrameParser()
    assert parser.feed(b"") == []


# --- back-compat generator ----------------------------------------------

def test_iter_liveview_frames_back_compat(cam):
    frames = list(cam.iter_liveview_frames())
    assert frames == LIVEVIEW_FRAMES


def test_iter_liveview_frames_max_frames(cam):
    frames = list(cam.iter_liveview_frames(max_frames=2))
    assert frames == LIVEVIEW_FRAMES[:2]


# --- liveview() context manager ------------------------------------------

def test_liveview_context_manager_yields_frames(cam):
    with cam.liveview() as stream:
        frames = list(stream)
    assert frames == LIVEVIEW_FRAMES


def test_liveview_context_manager_max_frames(cam):
    with cam.liveview(max_frames=2) as stream:
        frames = list(stream)
    assert frames == LIVEVIEW_FRAMES[:2]


def test_liveview_closes_on_normal_completion(cam, monkeypatch):
    resp = cam.liveview_stream()
    monkeypatch.setattr(cam, "liveview_stream", lambda: resp)
    with cam.liveview() as stream:
        list(stream)
    assert resp.closed is True


def test_liveview_closes_on_early_break(cam, monkeypatch):
    """The whole point of the context-manager API: breaking out of the frame
    loop early must still close the Response (drop the mirror) on __exit__,
    unlike bare iter_liveview_frames() whose cleanup depends on the
    generator being exhausted or garbage-collected."""
    resp = cam.liveview_stream()
    monkeypatch.setattr(cam, "liveview_stream", lambda: resp)
    with cam.liveview() as stream:
        for i, _frame in enumerate(stream):
            if i == 0:
                break
    assert resp.closed is True


def test_liveview_closes_on_exception_in_loop_body(cam, monkeypatch):
    resp = cam.liveview_stream()
    monkeypatch.setattr(cam, "liveview_stream", lambda: resp)
    with pytest.raises(ValueError):
        with cam.liveview() as stream:
            for _frame in stream:
                raise ValueError("boom")
    assert resp.closed is True


def test_liveview_iterating_outside_context_raises(cam):
    stream = cam.liveview()
    with pytest.raises(RuntimeError):
        list(stream)
