import json

import numpy as np
import pytest

import speakers


@pytest.fixture
def speakers_file(tmp_path, monkeypatch):
    path = str(tmp_path / "speakers.json")
    monkeypatch.setattr(speakers, "SPEAKERS_FILE", path)
    return path


def test_enroll_and_identify_same_vector(speakers_file):
    vec = np.array([1.0, 0.0, 0.0])
    speakers.enroll_speaker("Alice", vec)
    assert speakers.identify_speaker(vec) == "Alice"


def test_different_vector_below_threshold(speakers_file):
    speakers.enroll_speaker("Alice", np.array([1.0, 0.0, 0.0]))
    # Orthogonal vector -> cosine similarity 0
    assert speakers.identify_speaker(np.array([0.0, 1.0, 0.0])) is None


def test_two_speakers_picks_most_similar(speakers_file):
    speakers.enroll_speaker("Alice", np.array([1.0, 0.0, 0.0]))
    speakers.enroll_speaker("Bob", np.array([0.0, 1.0, 0.0]))
    # Closer to Bob
    query = np.array([0.1, 1.0, 0.0])
    assert speakers.identify_speaker(query) == "Bob"


def test_re_enroll_averages(speakers_file):
    v1 = np.array([1.0, 0.0, 0.0])
    v2 = np.array([0.0, 1.0, 0.0])
    speakers.enroll_speaker("Alice", v1)
    speakers.enroll_speaker("Alice", v2)
    # After averaging, the stored vector should be along (1,1,0) normalized
    # A query along (1,1,0) should match Alice
    query = np.array([1.0, 1.0, 0.0])
    assert speakers.identify_speaker(query) == "Alice"
    # Verify the stored vector is actually the normalized average
    data = json.loads(open(speakers_file).read())
    stored = np.array(data["speakers"]["Alice"])
    expected = (np.array([1.0, 0.0, 0.0]) + np.array([0.0, 1.0, 0.0]))
    expected = expected / np.linalg.norm(expected)
    np.testing.assert_allclose(stored, expected, atol=1e-10)


def test_old_format_file(speakers_file):
    # Old format: flat dict of label -> name string
    with open(speakers_file, "w") as f:
        json.dump({"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}, f)
    assert speakers.list_speakers() == []
    assert speakers.identify_speaker(np.array([1.0, 0.0])) is None


def test_corrupted_json(speakers_file):
    with open(speakers_file, "w") as f:
        f.write("{not valid json")
    assert speakers.list_speakers() == []
    assert speakers.identify_speaker(np.array([1.0, 0.0])) is None


def test_zero_vector_returns_none(speakers_file):
    speakers.enroll_speaker("Alice", np.array([1.0, 0.0, 0.0]))
    assert speakers.identify_speaker(np.array([0.0, 0.0, 0.0])) is None


def test_none_embedding_returns_none(speakers_file):
    speakers.enroll_speaker("Alice", np.array([1.0, 0.0, 0.0]))
    assert speakers.identify_speaker(None) is None


def test_list_speakers(speakers_file):
    speakers.enroll_speaker("Alice", np.array([1.0, 0.0]))
    speakers.enroll_speaker("Bob", np.array([0.0, 1.0]))
    assert set(speakers.list_speakers()) == {"Alice", "Bob"}
