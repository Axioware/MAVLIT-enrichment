"""Engagement rate of an Instagram profile: (average likes + average comments) / followers * 100."""


def _count(value) -> int | None:
    """A usable like/comment count: a number that is not negative (Instagram reports hidden counts as -1) and not a bool."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def engagement_rate(avg_likes, avg_comments, followers) -> float | None:
    """(average likes + average comments) / followers * 100, one decimal; None without likes or without followers."""
    if avg_likes is None or not followers or followers <= 0:
        return None
    return round((float(avg_likes) + float(avg_comments or 0)) / followers * 100, 1)


def engagement_rate_from_posts(posts: list[tuple], followers) -> float | None:
    """posts: (likes, comments) of each post. Posts whose likes are hidden are left out of the likes average, hidden comments of the comments average."""
    likes = [n for n in (_count(p[0]) for p in posts) if n is not None]
    comments = [n for n in (_count(p[1]) for p in posts) if n is not None]
    if not likes:
        return None
    return engagement_rate(sum(likes) / len(likes), sum(comments) / len(comments) if comments else 0, followers)
