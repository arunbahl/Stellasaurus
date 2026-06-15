import pytest

from stellasaurus.venues.sharding import shard


def test_shard_splits_into_chunks():
    ids = [str(i) for i in range(250)]
    shards = shard(ids, max_per_conn=100, max_conns=5)
    assert [len(s) for s in shards] == [100, 100, 50]


def test_shard_single_chunk():
    assert shard(["a", "b"], max_per_conn=100, max_conns=5) == [["a", "b"]]


def test_shard_empty_returns_one_empty():
    assert shard([], max_per_conn=100, max_conns=5) == [[]]


def test_shard_over_capacity_raises():
    ids = [str(i) for i in range(600)]
    with pytest.raises(ValueError):
        shard(ids, max_per_conn=100, max_conns=5)
