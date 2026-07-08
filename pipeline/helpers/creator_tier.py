def bucket_creator_tier(follower_count: int | None) -> str | None:
    """
    Bucket a follower/subscriber count into nano / micro / macro / mega.
    Returns None if the count is unknown (never classify unknown as nano).
    """
    if follower_count is None:
        return None
    if follower_count < 10_000:
        return "nano"
    if follower_count < 100_000:
        return "micro"
    if follower_count < 1_000_000:
        return "macro"
    return "mega"
