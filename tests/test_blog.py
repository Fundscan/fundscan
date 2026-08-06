"""
Tests for fundscan/blog.py -- the draft/published gating that keeps thin
placeholder content out of the public index and (via app.py) the sitemap.
"""
from fundscan.blog import POSTS, get_post, published_posts


def test_seed_posts_are_unpublished_by_default():
    """
    All three seeded posts ship as drafts -- this is the whole point of the
    scaffold (infra ready, content not yet written). If this ever flips
    true accidentally, thin placeholder copy would go live/indexed.
    """
    assert POSTS  # sanity: seed content exists
    assert all(p.published is False for p in POSTS)


def test_get_post_404s_for_unpublished_slug():
    draft = next(p for p in POSTS if not p.published)
    assert get_post(draft.slug) is None


def test_get_post_returns_published_post():
    post = POSTS[0]
    try:
        post.published = True
        post.published_at = "2026-01-01"
        assert get_post(post.slug) is post
    finally:
        post.published = False
        post.published_at = None


def test_get_post_unknown_slug_returns_none():
    assert get_post("this-slug-does-not-exist") is None


def test_published_posts_excludes_drafts_and_sorts_newest_first():
    a, b = POSTS[0], POSTS[1]
    try:
        a.published, a.published_at = True, "2026-01-01"
        b.published, b.published_at = True, "2026-06-01"
        result = published_posts()
        slugs = [p.slug for p in result]
        assert a.slug in slugs and b.slug in slugs
        assert slugs.index(b.slug) < slugs.index(a.slug)  # newer first
        # any post still marked draft must not appear
        assert all(p.published for p in result)
    finally:
        a.published, a.published_at = False, None
        b.published, b.published_at = False, None
