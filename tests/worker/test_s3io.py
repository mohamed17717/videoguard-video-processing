"""S3IO behaviour: conditional locks, storage classes, tags, upload trees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vg_worker.s3io import (
    S3IO,
    encode_tagging,
    make_tags,
    storage_class_for_object,
    tagging_tag_set,
)

from conftest import FakeS3, FakeS3Error


@pytest.fixture
def s3io(s3: FakeS3) -> S3IO:
    return S3IO(lambda: s3, "vg-media-dev")


def test_conditional_lock_acquires_then_fails(s3io: S3IO, s3: FakeS3) -> None:
    ok, etag = s3io.conditional_lock("reports/j/_LOCK", b'{"expires_at": 10}')
    assert ok is True
    assert etag is not None
    ok2, etag2 = s3io.conditional_lock("reports/j/_LOCK", b'{"expires_at": 20}')
    assert ok2 is False  # the lock exists -> 412 (another worker is live)
    assert etag2 is None


def test_read_lock_returns_body(s3io: S3IO) -> None:
    s3io.conditional_lock("reports/j/_LOCK", json.dumps({"expires_at": 44}).encode())
    body = s3io.read_lock("reports/j/_LOCK")
    assert body == {"expires_at": 44}


def test_renew_lock_ok_and_etag_rotation(s3io: S3IO) -> None:
    ok, etag = s3io.conditional_lock("reports/j/_LOCK", b'{"a":1}')
    assert ok and etag is not None
    new_ok, new_etag = s3io.renew_lock("reports/j/_LOCK", etag, b'{"expires_at": 99}')
    assert new_ok is True
    assert new_etag is not None and new_etag != etag


def test_renew_lock_fails_on_changed_etag(s3io: S3IO) -> None:
    s3io.conditional_lock("reports/j/_LOCK", b'{"a":1}')
    # IfMatch with an unknown ETag -> 412 -> (False, None): someone else won.
    other_ok, other = s3io.renew_lock("reports/j/_LOCK", '"another-instance-etag"', b'{"x":1}')
    assert other_ok is False
    assert other is None


def test_done_exists_false_then_true(s3io: S3IO) -> None:
    assert s3io.done_exists("reports/j/_DONE") is False
    s3io.write_done("reports/j/_DONE", b'{"completed": true}')
    assert s3io.done_exists("reports/j/_DONE") is True


def test_get_object_missing_raises_404(s3io: S3IO) -> None:
    with pytest.raises(FakeS3Error) as excinfo:
        s3io.get_object("nope")
    assert excinfo.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_mutually_exclusive_condition_flags_raise(s3io: S3IO) -> None:
    with pytest.raises(ValueError):
        s3io.put_object(
            "k",
            b"x",
            storage_class="STANDARD",
            tags={"a": "b"},
            if_none_match=True,
            if_match='"etag"',
        )


@pytest.mark.parametrize(
    ("kind", "retention", "expected"),
    [
        ("cmaf", "archive", "GLACIER_IR"),
        ("master", "archive", "GLACIER_IR"),
        ("cmaf", "ttl30", "STANDARD"),
        ("master", "ttl30", "STANDARD"),
        ("standard", "archive", "STANDARD"),
        ("standard", "ttl30", "STANDARD"),
    ],
)
def test_storage_class_choice_per_retention(kind: str, retention: str, expected: str) -> None:
    assert storage_class_for_object(kind, retention) == expected


def test_unknown_retention_raises() -> None:
    with pytest.raises(ValueError):
        storage_class_for_object("cmaf", "banana")


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        storage_class_for_object("banana", "archive")


def test_tags_and_tagging_encoding(s3io: S3IO, s3: FakeS3) -> None:
    tags = make_tags("archive", processed=True)
    assert tags == {"vg-retention": "archive", "processed": "true"}
    assert encode_tagging(make_tags("ttl30")) == "vg-retention=ttl30"
    tag_set = tagging_tag_set(tags)
    assert tag_set["TagSet"][0]["Key"] == "processed"
    s3io.write_done("reports/j/_DONE", b"{}")
    before = len(s3.events)
    s3io.put_tagging("reports/j/_DONE", tags)
    assert len(s3.events) == before + 1
    assert s3.events[-1][0] == "tag"


def test_upload_tree_uploads_sorted_with_prefix(s3io: S3IO, s3: FakeS3, tmp_path: Path) -> None:
    (tmp_path / "root" / "sub").mkdir(parents=True)
    (tmp_path / "root" / "a.jpg").write_bytes(b"AB")
    (tmp_path / "root" / "sub" / "b.jpg").write_bytes(b"CDE")
    results = s3io.upload_tree(
        tmp_path / "root",
        "renditions/j/thumbs/",
        storage_class="STANDARD",
        tags={"vg-retention": "archive"},
    )
    assert results == {
        "renditions/j/thumbs/a.jpg": 2,
        "renditions/j/thumbs/sub/b.jpg": 3,
    }
    assert s3.objects["renditions/j/thumbs/sub/b.jpg"].tagging == "vg-retention=archive"


def test_get_object_with_etag_is_one_get(s3io: S3IO, s3: FakeS3) -> None:
    s3.put("k", b"v")
    body, etag = s3io.get_object_with_etag("k")
    assert body == b"v"
    assert etag == s3.objects["k"].etag
    assert s3.events.count(("get", "k")) == 1


def test_put_file_streams_and_tags(s3io: S3IO, s3: FakeS3, tmp_path: Path) -> None:
    path = tmp_path / "master.mp4"
    path.write_bytes(b"x" * 32)
    s3io.put_file(
        "masters/j/variant_a.mp4",
        path,
        storage_class="GLACIER_IR",
        tags={"vg-retention": "archive"},
    )
    obj = s3.objects["masters/j/variant_a.mp4"]
    assert obj.data == b"x" * 32
    assert obj.storage_class == "GLACIER_IR"
    assert "vg-retention=archive" in obj.tagging


def test_expire_lock_is_conditional(s3io: S3IO, s3: FakeS3) -> None:
    etag = s3.put("reports/j/_LOCK", b'{"expires_at": 999}')
    assert s3io.expire_lock("reports/j/_LOCK", etag) is True
    assert json.loads(s3.objects["reports/j/_LOCK"].data)["expires_at"] == 0
    assert s3io.expire_lock("reports/j/_LOCK", '"stale"') is False


def test_get_json_or_none_maps_only_404(s3io: S3IO, s3: FakeS3) -> None:
    assert s3io.get_json_or_none("missing") is None
    s3.put("bad", b"not-json")
    with pytest.raises(ValueError):
        s3io.get_json_or_none("bad")
