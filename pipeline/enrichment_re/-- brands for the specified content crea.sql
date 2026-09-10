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
  WHERE ccr.id BETWEEN 128 AND 168
    AND tcbp.sponsorship_confidence >= 90
    AND br.refferls = false
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


-- count
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
  WHERE ccr.id BETWEEN 0 AND 168
    AND tcbp.sponsorship_confidence >= 90
    AND br.refferls = false
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


  -[ RECORD 1 ]-----+--------
niche             | Beauty
partnership_count | 47
-[ RECORD 2 ]-----+--------
niche             | Fitness
partnership_count | 19
-[ RECORD 3 ]-----+--------
niche             | Music
partnership_count | 18

-- remove other creator brands 
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
  WHERE ccr.id BETWEEN 128 AND 168
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