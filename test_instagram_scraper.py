import os
import json
from apify_client import ApifyClient

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------

APIFY_TOKEN = "apify_api_C9TSc0CmS3PLlny5n37pcNcQHLrDeg3ACh7Y"

ACTOR_ID = "shu8hvrXbJbY3Eb9W"

INPUT = {
    "addParentData": True,
    "directUrls": [
        "https://www.instagram.com/zer0lifestyle/"
    ],
    "onlyPostsNewerThan": "1 week",
    # "resultsLimit": 50,
    "resultsType": "posts"
}

# ------------------------------------------------------------------
# RUN ACTOR
# ------------------------------------------------------------------

client = ApifyClient(APIFY_TOKEN)

print("Running actor...")

run = client.actor(ACTOR_ID).call(
    run_input=INPUT
)

dataset_id = run["defaultDatasetId"]

print(f"Run ID: {run['id']}")
print(f"Dataset ID: {dataset_id}")

# ------------------------------------------------------------------
# LOAD RESULTS
# ------------------------------------------------------------------

items = list(
    client.dataset(dataset_id).iterate_items()
)

print(f"\nReturned {len(items)} posts/reels\n")

# ------------------------------------------------------------------
# BUILD CLEAN OUTPUT
# ------------------------------------------------------------------

results = []

for item in items:

    record = {
        # Profile
        "username": item.get("username"),
        "owner_username": item.get("ownerUsername"),
        "full_name": item.get("fullName"),
        "biography": item.get("biography"),
        "followers_count": item.get("followersCount"),
        "follows_count": item.get("followsCount"),
        "posts_count": item.get("postsCount"),
        "business_category_name": item.get("businessCategoryName"),
        "is_business_account": item.get("isBusinessAccount"),
        "verified": item.get("verified"),
        "external_url": item.get("externalUrl"),

        # Post
        "post_id": item.get("id"),
        "short_code": item.get("shortCode"),
        "post_url": item.get("url"),
        "timestamp": item.get("timestamp"),
        "type": item.get("type"),

        # Content
        "caption": item.get("caption"),
        "hashtags": item.get("hashtags"),

        # Collaboration Signals
        "mentions": item.get("mentions"),
        "tagged_users": item.get("taggedUsers"),
        "coauthor_producers": item.get("coauthorProducers"),
        "paid_partnership": item.get("paidPartnership"),
        "sponsors": item.get("sponsors"),

        # Engagement
        "likes_count": item.get("likesCount"),
        "comments_count": item.get("commentsCount"),
        "video_view_count": item.get("videoViewCount"),
        "video_play_count": item.get("videoPlayCount"),

        # Location
        "location_id": item.get("locationId"),
        "location_name": item.get("locationName"),

        # Raw
        "raw": item,
    }

    results.append(record)

# ------------------------------------------------------------------
# PRINT SUMMARY
# ------------------------------------------------------------------

for idx, post in enumerate(results, start=1):

    print("=" * 100)
    print(f"POST #{idx}")

    print("URL:", post["post_url"])
    print("TYPE:", post["type"])
    print("DATE:", post["timestamp"])

    print("LIKES:", post["likes_count"])
    print("COMMENTS:", post["comments_count"])

    print("MENTIONS:", post["mentions"])
    print("TAGGED USERS:", post["tagged_users"])
    print("COAUTHORS:", post["coauthor_producers"])
    print("PAID PARTNERSHIP:", post["paid_partnership"])
    print("SPONSORS:", post["sponsors"])

# ------------------------------------------------------------------
# SAVE TO JSON
# ------------------------------------------------------------------

with open("instagram_posts.json", "w", encoding="utf-8") as f:
    json.dump(
        results,
        f,
        indent=2,
        ensure_ascii=False,
        default=str,
    )

print(
    f"\nSaved {len(results)} records to instagram_posts.json"
)