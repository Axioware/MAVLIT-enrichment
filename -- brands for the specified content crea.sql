-- brands per niche for the specified content creators(BETWEEN 169 AND 182)(.csv query)
WITH best_per_brand AS (
  SELECT DISTINCT ON (tcbp.brand_raw_id)
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 1 AND 208
    AND tcbp.sponsorship_confidence >= 90
    AND br.refferls = false
    AND br.geo_reach_score IS NOT NULL
    AND br.geo_reach_score BETWEEN 0 AND 40
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST,
    tcbp.post_timestamp DESC NULLS LAST
)
SELECT
  creator_username,
  niche,
  brand_name,
  post_url,
  post_timestamp,
  sponsorship_confidence
FROM best_per_brand
ORDER BY
  niche ASC,
  brand_name ASC;


-- count for brands per niche for the specified content creators(BETWEEN 0 AND 182)
WITH best_per_brand AS (
  SELECT DISTINCT ON (tcbp.brand_raw_id)
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 1 AND 208
    AND tcbp.sponsorship_confidence >= 90
    AND br.refferls = false
    AND (
      br.geo_reach_score BETWEEN 0 AND 40
      OR br.geo_reach_score IS NULL
    )
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
  ORDER BY
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST,
    tcbp.post_timestamp DESC NULLS LAST
)
SELECT
  niche,
  COUNT(*) AS distinct_brand_count
FROM best_per_brand
WHERE niche IN ('Music', 'Beauty', 'Fitness', 'Health')
GROUP BY niche
ORDER BY distinct_brand_count DESC;

// Example output:
  -[ RECORD 1 ]-----+--------
niche             | Beauty
partnership_count | 47
-[ RECORD 2 ]-----+--------
niche             | Fitness
partnership_count | 19
-[ RECORD 3 ]-----+--------
niche             | Music
partnership_count | 18




-- same query as above but remove brands that are also associated with other content creatr
--example brand A is associated with content creator 1 and content creator 2, then brand A will be removed from the result set if you want to see creator 2
WITH best_per_brand AS (
  SELECT DISTINCT ON (tcbp.brand_raw_id)
    ccr.username AS creator_username,
    ccr.niche,
    tcbp.brand_name,
    tcbp.brand_raw_id,
    tcbp.post_url,
    tcbp.post_timestamp,
    tcbp.sponsorship_confidence
  FROM content_creator_re ccr
  JOIN test_creator_brand_partnership_posts tcbp
    ON tcbp.content_creator_re_id = ccr.id
  JOIN brands_raw br
    ON br.id = tcbp.brand_raw_id
  WHERE ccr.id BETWEEN 169 AND 182
    AND tcbp.sponsorship_confidence >= 90
    AND br.refferls = false
    AND tcbp.post_timestamp >= '2026-01-01'
    AND tcbp.post_timestamp < '2027-01-01'
    AND NOT EXISTS (
      SELECT 1
      FROM test_creator_brand_partnership_posts t2
      JOIN content_creator_re c2
        ON c2.id = t2.content_creator_re_id
      WHERE t2.brand_raw_id = tcbp.brand_raw_id
        AND c2.id NOT BETWEEN 128 AND 168
    )
  ORDER BY
    tcbp.brand_raw_id,
    tcbp.sponsorship_confidence DESC NULLS LAST,
    tcbp.post_timestamp DESC NULLS LAST
)
SELECT
  creator_username,
  niche,
  brand_name,
  post_url,
  post_timestamp,
  sponsorship_confidence
FROM best_per_brand
ORDER BY
  niche ASC,
  brand_name ASC;



SELECT DISTINCT niche
FROM brands_niches;

-- sudo -u postgres psql
-- \c mavlit_enrichment_test

-- sudo systemctl restart mavlit
-- sudo systemctl status mavlit


SELECT niche
FROM brands_niches
WHERE niche IS NOT NULL

UNION

SELECT niche
FROM instagram_users
WHERE niche IS NOT NULL

ORDER BY niche;


SELECT COUNT(DISTINCT username)
FROM instagram_users
WHERE niche IS NOT NULL;




-- To see why the other brands were excluded:from initial brand scoring 

SELECT
    COUNT(*) FILTER (WHERE NOT has_official_website) AS no_official_website,
    COUNT(*) FILTER (WHERE NOT shopify_checked) AS shopify_not_checked,
    COUNT(*) FILTER (WHERE NOT tranco_checked) AS tranco_not_checked,
    COUNT(*) FILTER (WHERE NOT youtube_checked) AS youtube_not_checked,
    COUNT(*) FILTER (WHERE NOT instagram_checked) AS instagram_not_checked,
    COUNT(*) FILTER (WHERE initial_brand_scored) AS already_scored
FROM brands_raw;
