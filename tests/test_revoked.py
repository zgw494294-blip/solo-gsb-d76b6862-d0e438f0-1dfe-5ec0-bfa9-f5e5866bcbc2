"""app/revoked.py 单测:包标识计算与本地撤销名单读取校验。"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.revoked import (
    RevocationListError,
    load_revoked_digests,
    package_digest,
)

HEADER_SEG = "eyJhbGciOiJFZERTQSIsImtpZCI6ImsifQ"
PAYLOAD_SEG = "eyJuYmYiOjAsImV4cCI6MSwicnVsZXMiOltdfQ"
DIGEST = hashlib.sha256(f"{HEADER_SEG}.{PAYLOAD_SEG}".encode("ascii")).hexdigest()


def test_package_digest_sha256_of_raw_first_two_segments():
    assert package_digest(HEADER_SEG, PAYLOAD_SEG) == DIGEST
    assert len(DIGEST) == 64
    assert DIGEST == DIGEST.lower()


def test_package_digest_is_ascii_dot_join():
    expected = hashlib.sha256((HEADER_SEG + "." + PAYLOAD_SEG).encode("ascii")).hexdigest()
    assert package_digest(HEADER_SEG, PAYLOAD_SEG) == expected


def test_unconfigured_path_is_error(tmp_path, monkeypatch):
    # 调用方(signed.py)仅在路径非空时调用;空路径本身视为配置错误
    with pytest.raises(RevocationListError):
        load_revoked_digests("")


def test_empty_list_loads(tmp_path):
    path = tmp_path / "revoked.json"
    path.write_text(json.dumps({"revoked": []}), encoding="utf-8")
    assert load_revoked_digests(str(path)) == frozenset()


def test_list_with_duplicates_loads_as_set(tmp_path):
    path = tmp_path / "revoked.json"
    path.write_text(
        json.dumps({"revoked": [DIGEST, DIGEST]}), encoding="utf-8"
    )
    assert load_revoked_digests(str(path)) == frozenset({DIGEST})


def test_missing_file_is_error(tmp_path):
    with pytest.raises(RevocationListError, match="不存在"):
        load_revoked_digests(str(tmp_path / "missing.json"))


def test_directory_path_is_error(tmp_path):
    # open 目录在 Linux 上抛 IsADirectoryError(OSError 子类)
    with pytest.raises(RevocationListError, match="无法读取"):
        load_revoked_digests(str(tmp_path))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",  # 顶层不是对象
        "{}",  # 缺 revoked
        '{"revoked": [],"other": []}',  # 额外键
        '{"revoked": {}}',  # revoked 不是数组
        '{"revoked": [1]}',  # 元素不是字符串
        '{"revoked": [null]}',
        '{"revoked": ["ABC"]}',  # 非十六进制
        '{"revoked": ["' + "g" * 64 + '"]}',  # 超范围十六进制字母
        '{"revoked": ["' + "A" + "0" * 63 + '"]}',  # 64 位但含大写
        '{"revoked": ["' + "0" * 63 + '"]}',  # 长度不足
        '{"revoked": ["' + "0" * 65 + '"]}',  # 长度超长
    ],
)
def test_invalid_structures_raise(tmp_path, content):
    path = tmp_path / "revoked.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RevocationListError):
        load_revoked_digests(str(path))


def test_reread_on_every_call_picks_up_atomic_replace(tmp_path, monkeypatch):
    import os

    path = tmp_path / "revoked.json"
    path.write_text(json.dumps({"revoked": []}), encoding="utf-8")
    assert load_revoked_digests(str(path)) == frozenset()

    tmp = tmp_path / "revoked.tmp"
    tmp.write_text(json.dumps({"revoked": [DIGEST]}), encoding="utf-8")
    os.replace(tmp, path)  # 原子替换

    # 无缓存:再次调用立即看到新名单
    assert load_revoked_digests(str(path)) == frozenset({DIGEST})
