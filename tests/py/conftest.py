"""Shared pytest fixtures: an in-memory database and a Notification factory."""

from __future__ import annotations

import itertools

import pytest

from inbox_sift.models import Notification, make_id
from inbox_sift.storage.db import Database

# Sensible defaults so a test only names the fields it cares about.
DEFAULTS: dict = {
    "host": "github.com",
    "kind": "pr",
    "number": "#1",
    "title": "Fix the flaky sync test",
    "repo": "acme/app",
    "reason": "subscribed",
    "actor": "mlee",
    "actor_role": "author",
    "unread": True,
    "updated_at": "2026-01-01T00:00:00Z",
}


@pytest.fixture
def db():
    database = Database(":memory:")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def make_notif():
    """Factory: ``make_notif(repo='acme/infra', reason='mention')`` -> Notification.

    Thread IDs (and therefore ids) are unique per call unless you pass one explicitly.
    """
    counter = itertools.count(1)

    def make(**overrides) -> Notification:
        data = dict(DEFAULTS)
        data["thread_id"] = str(next(counter))
        data.update(overrides)
        if "id" not in overrides:
            data["id"] = make_id(data["host"], data["thread_id"])
        return Notification(**data)

    return make
